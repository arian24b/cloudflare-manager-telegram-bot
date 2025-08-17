import asyncio
import logging
import os
import json
from typing import Dict, List, Optional, Any
from datetime import datetime

import cloudflare
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, ContextTypes, filters, ConversationHandler
)
from sqlalchemy import create_engine, Column, String, DateTime, Text, Integer
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
Base = declarative_base()

class Tenant(Base):
    __tablename__ = 'tenants'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    cloudflare_token = Column(String, nullable=False)
    admin_user_id = Column(String, nullable=False)  # Tenant admin
    description = Column(Text)
    is_active = Column(String, default='true')
    created_at = Column(DateTime, default=datetime.utcnow)

class DomainGroup(Base):
    __tablename__ = 'domain_groups'
    
    id = Column(String, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    domains = Column(Text)  # JSON string of domain IDs
    created_at = Column(DateTime, default=datetime.utcnow)

class UserSession(Base):
    __tablename__ = 'user_sessions'
    
    user_id = Column(String, primary_key=True)
    current_tenant = Column(Integer)
    current_domain = Column(String)
    current_group = Column(String)
    last_activity = Column(DateTime, default=datetime.utcnow)

class BotConfig(Base):
    __tablename__ = 'bot_config'
    
    key = Column(String, primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow)

# Conversation states
(WAITING_TENANT_NAME, WAITING_TENANT_DESCRIPTION, WAITING_CF_TOKEN, WAITING_TENANT_ADMIN,
 WAITING_GROUP_NAME, WAITING_GROUP_DESCRIPTION, WAITING_DOMAIN_SELECTION,
 WAITING_RECORD_TYPE, WAITING_RECORD_NAME, WAITING_RECORD_CONTENT,
 WAITING_RECORD_TTL, WAITING_RECORD_PRIORITY, WAITING_SUPER_ADMIN_ID) = range(13)

class CloudflareDNSBot:
    def __init__(self, telegram_token: str, db_url: str, super_admin_id: str = None):
        self.telegram_token = telegram_token
        self.super_admin_id = super_admin_id
        
        # Database setup
        self.engine = create_async_engine(db_url)
        self.SessionLocal = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        
        # Cache for tenants and their domains
        self.tenants_cache: Dict[int, Dict] = {}
        self.current_tenant_id: Optional[int] = None
        
    async def init_db(self):
        """Initialize database tables"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def is_super_admin(self, user_id: int) -> bool:
        """Check if user is super admin"""
        if not self.super_admin_id:
            # If no super admin set, get from config or first-time setup
            async with self.SessionLocal() as session:
                result = await session.execute(
                    "SELECT value FROM bot_config WHERE key = 'super_admin_id'"
                )
                admin_config = result.fetchone()
                if admin_config:
                    self.super_admin_id = admin_config[0]
                    return str(user_id) == self.super_admin_id
                else:
                    # First time setup - this user becomes super admin
                    await self.set_config('super_admin_id', str(user_id))
                    self.super_admin_id = str(user_id)
                    return True
        
        return str(user_id) == self.super_admin_id

    async def is_tenant_admin(self, user_id: int, tenant_id: int = None) -> bool:
        """Check if user is admin of a specific tenant or any tenant"""
        async with self.SessionLocal() as session:
            if tenant_id:
                # Check specific tenant
                result = await session.execute(
                    "SELECT admin_user_id FROM tenants WHERE id = ? AND is_active = 'true'",
                    (tenant_id,)
                )
                tenant = result.fetchone()
                return tenant and tenant[0] == str(user_id)
            else:
                # Check if user is admin of any tenant
                result = await session.execute(
                    "SELECT COUNT(*) FROM tenants WHERE admin_user_id = ? AND is_active = 'true'",
                    (str(user_id),)
                )
                count = result.fetchone()
                return count and count[0] > 0

    async def get_user_tenants(self, user_id: int) -> List[Tenant]:
        """Get tenants that user can manage"""
        async with self.SessionLocal() as session:
            result = await session.execute(
                "SELECT * FROM tenants WHERE admin_user_id = ? AND is_active = 'true'",
                (str(user_id),)
            )
            return [Tenant(
                id=row[0], name=row[1], cloudflare_token=row[2], admin_user_id=row[3],
                description=row[4], is_active=row[5], created_at=row[6]
            ) for row in result.fetchall()]

    async def has_access(self, user_id: int, tenant_id: int = None) -> bool:
        """Check if user has access (super admin or tenant admin)"""
        if await self.is_super_admin(user_id):
            return True
        if tenant_id:
            return await self.is_tenant_admin(user_id, tenant_id)
        return await self.is_tenant_admin(user_id)

    async def set_config(self, key: str, value: str):
        """Set configuration value"""
        async with self.SessionLocal() as session:
            config = BotConfig(key=key, value=value, updated_at=datetime.utcnow())
            await session.merge(config)
            await session.commit()

    async def get_config(self, key: str) -> Optional[str]:
        """Get configuration value"""
        async with self.SessionLocal() as session:
            result = await session.execute(
                "SELECT value FROM bot_config WHERE key = ?", (key,)
            )
            config = result.fetchone()
            return config[0] if config else None

    async def get_tenants(self, user_id: int = None) -> List[Tenant]:
        """Get tenants (all for super admin, only owned for tenant admin)"""
        async with self.SessionLocal() as session:
            if user_id and not await self.is_super_admin(user_id):
                # Tenant admin - only their tenants
                result = await session.execute(
                    "SELECT * FROM tenants WHERE admin_user_id = ? AND is_active = 'true'",
                    (str(user_id),)
                )
            else:
                # Super admin - all tenants
                result = await session.execute("SELECT * FROM tenants WHERE is_active = 'true'")
            
            return [Tenant(
                id=row[0], name=row[1], cloudflare_token=row[2], admin_user_id=row[3],
                description=row[4], is_active=row[5], created_at=row[6]
            ) for row in result.fetchall()]

    async def get_tenant_by_id(self, tenant_id: int) -> Optional[Tenant]:
        """Get tenant by ID"""
        async with self.SessionLocal() as session:
            result = await session.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,))
            row = result.fetchone()
            if row:
                return Tenant(
                    id=row[0], name=row[1], cloudflare_token=row[2], admin_user_id=row[3],
                    description=row[4], is_active=row[5], created_at=row[6]
                )
            return None

    async def refresh_tenant_domains(self, tenant_id: int):
        """Refresh domains cache for a specific tenant"""
        tenant = await self.get_tenant_by_id(tenant_id)
        if not tenant:
            return
        
        try:
            cf = cloudflare.Cloudflare(api_token=tenant.cloudflare_token)
            zones = await asyncio.to_thread(cf.zones.list)
            
            self.tenants_cache[tenant_id] = {
                'tenant': tenant,
                'domains': {zone.name: zone for zone in zones},
                'zones': {zone.id: zone for zone in zones}
            }
            
            logger.info(f"Refreshed cache for tenant {tenant.name} with {len(zones)} domains")
        except Exception as e:
            logger.error(f"Error refreshing domains for tenant {tenant_id}: {e}")
            raise e

    async def get_current_cf_client(self) -> Optional[cloudflare.Cloudflare]:
        """Get Cloudflare client for current tenant"""
        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            return None
        
        tenant = self.tenants_cache[self.current_tenant_id]['tenant']
        return cloudflare.Cloudflare(api_token=tenant.cloudflare_token)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        
        # Check access level
        is_super = await self.is_super_admin(user_id)
        is_tenant_admin = await self.is_tenant_admin(user_id)
        
        if not is_super and not is_tenant_admin:
            await update.message.reply_text(
                "❌ *Access Denied*\n\n"
                "This bot is private. You need to be assigned as:\n"
                "• Super Admin (bot management)\n"
                "• Tenant Admin (domain management)\n\n"
                "Contact the super admin for access.",
                parse_mode='Markdown'
            )
            return
        
        # Get user's accessible tenants
        tenants = await self.get_tenants(user_id)
        user_role = "🔧 Super Admin" if is_super else "👤 Tenant Admin"
        
        keyboard = []
        if tenants:
            if len(tenants) == 1 and not is_super:
                # Auto-select single tenant for tenant admin
                self.current_tenant_id = tenants[0].id
                await self.refresh_tenant_domains(self.current_tenant_id)
            
            keyboard.extend([
                [InlineKeyboardButton("🏢 Switch Tenant", callback_data="switch_tenant")],
                [InlineKeyboardButton("🌐 View Domains", callback_data="view_domains")],
                [InlineKeyboardButton("🔧 Manage DNS Records", callback_data="manage_dns")],
                [InlineKeyboardButton("📁 Domain Groups", callback_data="manage_groups")],
            ])
        
        # Super admin only options
        if is_super:
            keyboard.append([InlineKeyboardButton("⚙️ Bot Settings", callback_data="bot_settings")])
        
        keyboard.append([InlineKeyboardButton("🔄 Refresh Cache", callback_data="refresh_cache")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        current_tenant_info = ""
        if self.current_tenant_id and self.current_tenant_id in self.tenants_cache:
            tenant = self.tenants_cache[self.current_tenant_id]['tenant']
            domain_count = len(self.tenants_cache[self.current_tenant_id]['domains'])
            current_tenant_info = f"\n🏢 *Current Tenant:* {tenant.name} ({domain_count} domains)"
        
        welcome_text = (
            "🚀 *Multi-Tenant Cloudflare DNS Manager*\n\n"
            f"👤 *User:* {update.effective_user.first_name}\n"
            f"🔐 *Role:* {user_role}\n"
            f"🏢 *Accessible Tenants:* {len(tenants)}"
            f"{current_tenant_info}\n\n"
            "🌟 *Features:*\n"
            "• Multi-tenant Cloudflare management\n"
            "• Role-based access control\n"
            "• Complete DNS record CRUD operations\n"
            "• Domain grouping and bulk operations\n\n"
            "Choose an option below:"
        )
        
        await update.message.reply_text(
            welcome_text, 
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    async def bot_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show bot settings menu (super admin only)"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        if not await self.is_super_admin(user_id):
            await query.edit_message_text("❌ Access denied. Super admin only.")
            return
        
        keyboard = [
            [InlineKeyboardButton("🏢 Manage All Tenants", callback_data="manage_all_tenants")],
            [InlineKeyboardButton("👤 Change Super Admin", callback_data="change_super_admin")],
            [InlineKeyboardButton("📊 System Stats", callback_data="system_stats")],
            [InlineKeyboardButton("🔧 Bot Configuration", callback_data="bot_config")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        async with self.SessionLocal() as session:
            result = await session.execute("SELECT COUNT(*) FROM tenants WHERE is_active = 'true'")
            tenant_count = result.fetchone()[0]
            
            result = await session.execute("SELECT COUNT(DISTINCT admin_user_id) FROM tenants WHERE is_active = 'true'")
            admin_count = result.fetchone()[0]
        
        super_admin_id = await self.get_config('super_admin_id')
        
        settings_text = (
            "⚙️ *Bot Settings (Super Admin)*\n\n"
            f"👑 *Super Admin ID:* `{super_admin_id}`\n"
            f"🏢 *Total Tenants:* {tenant_count}\n"
            f"👤 *Tenant Admins:* {admin_count}\n"
            f"💾 *Database:* Connected\n\n"
            "Select an option to configure:"
        )
        
        await query.edit_message_text(
            settings_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    async def manage_all_tenants(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manage all tenants (super admin only)"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        if not await self.is_super_admin(user_id):
            await query.edit_message_text("❌ Access denied. Super admin only.")
            return
        
        # Get all tenants
        async with self.SessionLocal() as session:
            result = await session.execute("SELECT * FROM tenants WHERE is_active = 'true'")
            tenants = [Tenant(
                id=row[0], name=row[1], cloudflare_token=row[2], admin_user_id=row[3],
                description=row[4], is_active=row[5], created_at=row[6]
            ) for row in result.fetchall()]
        
        keyboard = [
            [InlineKeyboardButton("➕ Add New Tenant", callback_data="add_tenant")]
        ]
        
        tenants_text = "🏢 *All Tenants Management*\n\n"
        
        if tenants:
            for tenant in tenants:
                status = "🟢" if tenant.is_active == 'true' else "🔴"
                tenants_text += f"{status} **{tenant.name}**\n"
                tenants_text += f"   👤 Admin: `{tenant.admin_user_id}`\n"
                if tenant.description:
                    tenants_text += f"   📝 {tenant.description}\n"
                tenants_text += "\n"
                
                keyboard.append([
                    InlineKeyboardButton(f"✏️ {tenant.name}", callback_data=f"edit_tenant:{tenant.id}"),
                    InlineKeyboardButton(f"🗑️", callback_data=f"delete_tenant:{tenant.id}")
                ])
        else:
            tenants_text += "No tenants configured yet.\n\nAdd your first tenant to start managing domains."
        
        keyboard.append([InlineKeyboardButton("🔙 Back to Settings", callback_data="bot_settings")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            tenants_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    async def add_tenant_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start adding new tenant (super admin only)"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        if not await self.is_super_admin(user_id):
            await query.edit_message_text("❌ Access denied. Super admin only.")
            return
        
        await query.edit_message_text(
            "🏢 *Add New Tenant*\n\n"
            "Enter the tenant name (e.g., 'Production', 'Client ABC', 'Personal'):",
            parse_mode='Markdown'
        )
        
        return WAITING_TENANT_NAME

    async def handle_tenant_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tenant name input"""
        tenant_name = update.message.text.strip()
        context.user_data['tenant_name'] = tenant_name
        
        await update.message.reply_text(
            f"🏢 *Tenant: {tenant_name}*\n\n"
            "Enter a description (optional, or type 'skip'):",
            parse_mode='Markdown'
        )
        
        return WAITING_TENANT_DESCRIPTION

    async def handle_tenant_description(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tenant description input"""
        description = update.message.text.strip()
        if description.lower() == 'skip':
            description = ""
        
        context.user_data['tenant_description'] = description
        
        await update.message.reply_text(
            "👤 *Tenant Admin*\n\n"
            "Enter the Telegram User ID of the tenant admin:\n\n"
            "ℹ️ *How to get User ID:*\n"
            "• Forward a message from the user to @userinfobot\n"
            "• Or ask them to send /start to @userinfobot",
            parse_mode='Markdown'
        )
        
        return WAITING_TENANT_ADMIN

    async def handle_tenant_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tenant admin input"""
        try:
            admin_user_id = int(update.message.text.strip())
            context.user_data['tenant_admin'] = str(admin_user_id)
        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid numeric User ID.",
                parse_mode='Markdown'
            )
            return WAITING_TENANT_ADMIN
        
        await update.message.reply_text(
            "🔐 *Cloudflare API Token*\n\n"
            "Enter the Cloudflare API Token for this tenant:\n\n"
            "ℹ️ *Required permissions:*\n"
            "• Zone:Zone:Read\n"
            "• Zone:DNS:Edit\n\n"
            "🔗 Get token at: https://dash.cloudflare.com/profile/api-tokens",
            parse_mode='Markdown'
        )
        
        return WAITING_CF_TOKEN

    async def handle_cf_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Cloudflare token input and create tenant"""
        cf_token = update.message.text.strip()
        
        # Test the token
        try:
            cf = cloudflare.Cloudflare(api_token=cf_token)
            zones = await asyncio.to_thread(cf.zones.list)
            
            # Create tenant
            tenant_name = context.user_data['tenant_name']
            tenant_description = context.user_data['tenant_description']
            tenant_admin = context.user_data['tenant_admin']
            
            async with self.SessionLocal() as session:
                # Insert tenant
                result = await session.execute(
                    "INSERT INTO tenants (name, cloudflare_token, admin_user_id, description, is_active) VALUES (?, ?, ?, ?, 'true')",
                    (tenant_name, cf_token, tenant_admin, tenant_description)
                )
                await session.commit()
                
                # Get the new tenant ID
                tenant_id = result.lastrowid
            
            # Refresh cache for new tenant
            await self.refresh_tenant_domains(tenant_id)
            
            success_text = (
                "✅ *Tenant Created Successfully!*\n\n"
                f"🏢 **Name:** {tenant_name}\n"
                f"👤 **Admin:** `{tenant_admin}`\n"
                f"📄 **Description:** {tenant_description or 'None'}\n"
                f"🌐 **Domains Found:** {len(zones)}\n"
                f"🆔 **Tenant ID:** {tenant_id}\n\n"
                f"The tenant admin (`{tenant_admin}`) can now access this tenant."
            )
            
            keyboard = [
                [InlineKeyboardButton(f"🔄 Switch to {tenant_name}", callback_data=f"set_tenant:{tenant_id}")],
                [InlineKeyboardButton("🏢 Manage Tenants", callback_data="manage_all_tenants")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                success_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"Error testing Cloudflare token: {e}")
            await update.message.reply_text(
                f"❌ **Invalid Cloudflare Token**\n\n"
                f"Error: `{str(e)}`\n\n"
                "Please check your token and try again:",
                parse_mode='Markdown'
            )
            return WAITING_CF_TOKEN
        
        context.user_data.clear()
        return ConversationHandler.END

    async def switch_tenant_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show tenant switching menu"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        tenants = await self.get_tenants(user_id)
        
        if not tenants:
            await query.edit_message_text(
                "❌ No accessible tenants.\n\nContact the super admin for access.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")
                ]])
            )
            return
        
        keyboard = []
        current_marker = ""
        
        for tenant in tenants:
            if tenant.id == self.current_tenant_id:
                current_marker = " ✅"
            else:
                current_marker = ""
                
            # Show if user is admin of this tenant
            admin_marker = " 👤" if tenant.admin_user_id == str(user_id) else ""
            
            keyboard.append([InlineKeyboardButton(
                f"🏢 {tenant.name}{current_marker}{admin_marker}",
                callback_data=f"set_tenant:{tenant.id}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        is_super = await self.is_super_admin(user_id)
        role_info = " (Super Admin)" if is_super else " (Tenant Admin)"
        
        switch_text = (
            f"🏢 *Switch Tenant{role_info}*\n\n"
            "Select a tenant to switch to:\n"
            "✅ = Currently active\n"
            "👤 = You are admin"
        )
        
        await query.edit_message_text(
            switch_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    async def set_tenant(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set current tenant"""
        query = update.callback_query
        await query.answer()
        
        tenant_id = int(query.data.split(':')[1])
        user_id = update.effective_user.id
        
        # Check access
        if not await self.has_access(user_id, tenant_id):
            await query.edit_message_text("❌ Access denied to this tenant.")
            return
        
        try:
            await self.refresh_tenant_domains(tenant_id)
            self.current_tenant_id = tenant_id
            
            tenant = self.tenants_cache[tenant_id]['tenant']
            domain_count = len(self.tenants_cache[tenant_id]['domains'])
            
            is_admin = tenant.admin_user_id == str(user_id)
            role_text = "👤 Tenant Admin" if is_admin else "🔧 Super Admin"
            
            await query.edit_message_text(
                f"✅ *Switched to: {tenant.name}*\n\n"
                f"🔐 *Your Role:* {role_text}\n"
                f"🌐 *Domains:* {domain_count}\n\n"
                "You can now manage domains and DNS records for this tenant.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🌐 View Domains", callback_data="view_domains"),
                    InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")
                ]])
            )
            
        except Exception as e:
            logger.error(f"Error switching to tenant {tenant_id}: {e}")
            await query.edit_message_text(
                f"❌ **Error switching tenant:**\n`{str(e)}`\n\n"
                "Please check the Cloudflare token for this tenant.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="switch_tenant")
                ]])
            )

    async def view_domains(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Display domains for current tenant"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        
        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            await query.edit_message_text(
                "❌ No tenant selected.\n\nPlease select a tenant first.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏢 Switch Tenant", callback_data="switch_tenant"),
                    InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")
                ]])
            )
            return
        
        # Check access to current tenant
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied to current tenant.")
            return
        
        tenant_data = self.tenants_cache[self.current_tenant_id]
        domains = tenant_data['domains']
        tenant = tenant_data['tenant']
        
        if not domains:
            await query.edit_message_text(
                f"❌ No domains found for tenant: {tenant.name}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Refresh", callback_data="refresh_cache"),
                    InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")
                ]])
            )
            return
        
        is_admin = tenant.admin_user_id == str(user_id)
        role_text = "👤 Admin" if is_admin else "🔧 Super Admin"
        
        domains_text = f"🌐 *{tenant.name} Domains ({role_text}):*\n\n"
        keyboard = []
        
        for i, (domain_name, zone) in enumerate(domains.items(), 1):
            status = "🟢" if zone.status == "active" else "🟡"
            domains_text += f"{i}. {status} `{domain_name}`\n"
            keyboard.append([InlineKeyboardButton(
                f"📝 {domain_name}", 
                callback_data=f"select_domain:{zone.id}"
            )])
        
        keyboard.extend([
            [InlineKeyboardButton("🏢 Switch Tenant", callback_data="switch_tenant")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
        ])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            domains_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    async def select_domain(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle domain selection"""
        query = update.callback_query
        await query.answer()
        
        zone_id = query.data.split(':')[1]
        user_id = update.effective_user.id
        
        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            await query.edit_message_text("❌ No tenant selected.")
            return
        
        # Check access to current tenant
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied to current tenant.")
            return
        
        zones = self.tenants_cache[self.current_tenant_id]['zones']
        zone = zones.get(zone_id)
        
        if not zone:
            await query.edit_message_text("❌ Domain not found.")
            return
        
        keyboard = [
            [InlineKeyboardButton("📋 View DNS Records", callback_data=f"view_records:{zone_id}")],
            [InlineKeyboardButton("➕ Add DNS Record", callback_data=f"add_record:{zone_id}")],
            [InlineKeyboardButton("🗑️ Delete DNS Records", callback_data=f"delete_menu:{zone_id}")],
            [InlineKeyboardButton("🔙 Back to Domains", callback_data="view_domains")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        tenant = self.tenants_cache[self.current_tenant_id]['tenant']
        is_admin = tenant.admin_user_id == str(user_id)
        role_text = "👤 Tenant Admin" if is_admin else "🔧 Super Admin"
        
        domain_info = (
            f"🌐 *Domain: {zone.name}*\n"
            f"🏢 *Tenant: {tenant.name}*\n"
            f"🔐 *Your Role: {role_text}*\n\n"
            f"📊 *Status:* {'🟢 Active' if zone.status == 'active' else '🟡 Pending'}\n"
            f"🆔 *Zone ID:* `{zone.id}`\n\n"
            "What would you like to do?"
        )
        
        await query.edit_message_text(
            domain_info,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    async def view_dns_records(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View DNS records for a domain"""
        query = update.callback_query
        await query.answer()
        
        zone_id = query.data.split(':')[1]
        user_id = update.effective_user.id
        
        # Check access
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return
        
        cf = await self.get_current_cf_client()
        if not cf:
            await query.edit_message_text("❌ No active tenant selected.")
            return
        
        zones = self.tenants_cache[self.current_tenant_id]['zones']
        zone = zones.get(zone_id)
        
        try:
            records = await asyncio.to_thread(cf.dns.records.list, zone_id=zone_id)
            
            if not records:
                records_text = f"📋 *DNS Records for {zone.name}*\n\n❌ No DNS records found."
            else:
                records_text = f"📋 *DNS Records for {zone.name}*\n\n"
                
                # Group records by type
                record_types = {}
                for record in records:
                    if record.type not in record_types:
                        record_types[record.type] = []
                    record_types[record.type].append(record)
                
                for record_type, type_records in record_types.items():
                    records_text += f"*{record_type} Records:*\n"
                    for record in type_records:
                        content = record.content
                        if len(content) > 50:
                            content = content[:47] + "..."
                        records_text += f"  • `{record.name}` → `{content}`\n"
                    records_text += "\n"
            
            keyboard = [
                [InlineKeyboardButton("➕ Add Record", callback_data=f"add_record:{zone_id}")],
                [InlineKeyboardButton("🗑️ Delete Records", callback_data=f"delete_menu:{zone_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data=f"select_domain:{zone_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                records_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"Error fetching DNS records: {e}")
            await query.edit_message_text(f"❌ Error fetching DNS records: {str(e)}")

    async def add_record_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start adding a new DNS record"""
        query = update.callback_query
        await query.answer()
        
        zone_id = query.data.split(':')[1]
        user_id = update.effective_user.id
        
        # Check access
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return
        
        context.user_data['zone_id'] = zone_id
        
        # DNS record types
        record_types = [
            [InlineKeyboardButton("A", callback_data="record_type:A"),
             InlineKeyboardButton("AAAA", callback_data="record_type:AAAA")],
            [InlineKeyboardButton("CNAME", callback_data="record_type:CNAME"),
             InlineKeyboardButton("MX", callback_data="record_type:MX")],
            [InlineKeyboardButton("TXT", callback_data="record_type:TXT"),
             InlineKeyboardButton("SRV", callback_data="record_type:SRV")],
            [InlineKeyboardButton("NS", callback_data="record_type:NS"),
             InlineKeyboardButton("PTR", callback_data="record_type:PTR")],
            [InlineKeyboardButton("🔙 Cancel", callback_data=f"select_domain:{zone_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(record_types)
        
        await query.edit_message_text(
            "📝 *Add New DNS Record*\n\nSelect the record type:",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        
        return WAITING_RECORD_TYPE

    async def handle_record_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle record type selection"""
        query = update.callback_query
        await query.answer()
        
        record_type = query.data.split(':')[1]
        context.user_data['record_type'] = record_type
        
        zone_id = context.user_data['zone_id']
        zones = self.tenants_cache[self.current_tenant_id]['zones']
        zone = zones.get(zone_id)
        
        await query.edit_message_text(
            f"📝 *Adding {record_type} Record for {zone.name}*\n\n"
            f"Enter the record name (subdomain):\n\n"
            f"Examples:\n"
            f"• `www` for www.{zone.name}\n"
            f"• `@` for {zone.name}\n"
            f"• `mail` for mail.{zone.name}",
            parse_mode='Markdown'
        )
        
        return WAITING_RECORD_NAME

    async def handle_record_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle record name input"""
        record_name = update.message.text.strip()
        context.user_data['record_name'] = record_name
        
        record_type = context.user_data['record_type']
        zone_id = context.user_data['zone_id']
        zones = self.tenants_cache[self.current_tenant_id]['zones']
        zone = zones.get(zone_id)
        
        # Provide type-specific content examples
        examples = {
            'A': 'IPv4 address (e.g., 192.168.1.1)',
            'AAAA': 'IPv6 address (e.g., 2001:db8::1)',
            'CNAME': 'Target domain (e.g., example.com)',
            'MX': 'Mail server (e.g., mail.example.com)',
            'TXT': 'Text content (e.g., "v=spf1 include:_spf.google.com ~all")',
            'SRV': 'Target (e.g., 10 5 443 target.example.com)',
            'NS': 'Name server (e.g., ns1.example.com)',
            'PTR': 'Target domain (e.g., example.com)'
        }
        
        full_name = record_name if record_name == '@' else f"{record_name}.{zone.name}"
        
        await update.message.reply_text(
            f"📝 *Adding {record_type} Record*\n\n"
            f"Name: `{full_name}`\n\n"
            f"Enter the record content:\n"
            f"*{examples.get(record_type, 'Record content')}*",
            parse_mode='Markdown'
        )
        
        return WAITING_RECORD_CONTENT

    async def handle_record_content(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle record content input"""
        record_content = update.message.text.strip()
        context.user_data['record_content'] = record_content
        
        record_type = context.user_data['record_type']
        
        # For MX and SRV records, ask for priority
        if record_type in ['MX', 'SRV']:
            await update.message.reply_text(
                f"📝 *Priority for {record_type} Record*\n\n"
                f"Enter the priority (lower number = higher priority):\n"
                f"Common values: 10, 20, 30",
                parse_mode='Markdown'
            )
            return WAITING_RECORD_PRIORITY
        else:
            # Skip priority and go to TTL
            await update.message.reply_text(
                "⏱️ *TTL (Time To Live)*\n\n"
                "Enter TTL in seconds (or type 'auto' for automatic):\n\n"
                "Common values:\n"
                "• 300 (5 minutes)\n"
                "• 3600 (1 hour)\n"
                "• 86400 (24 hours)",
                parse_mode='Markdown'
            )
            return WAITING_RECORD_TTL

    async def handle_record_priority(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle record priority input"""
        try:
            priority = int(update.message.text.strip())
            context.user_data['record_priority'] = priority
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid number for priority.")
            return WAITING_RECORD_PRIORITY
        
        await update.message.reply_text(
            "⏱️ *TTL (Time To Live)*\n\n"
            "Enter TTL in seconds (or type 'auto' for automatic):\n\n"
            "Common values:\n"
            "• 300 (5 minutes)\n"
            "• 3600 (1 hour)\n"
            "• 86400 (24 hours)",
            parse_mode='Markdown'
        )
        
        return WAITING_RECORD_TTL

    async def handle_record_ttl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle TTL input and create the record"""
        ttl_input = update.message.text.strip().lower()
        
        if ttl_input == 'auto':
            ttl = 1  # Cloudflare auto TTL
        else:
            try:
                ttl = int(ttl_input)
                if ttl < 120:
                    ttl = 120  # Minimum TTL
            except ValueError:
                await update.message.reply_text("❌ Please enter a valid number for TTL or 'auto'.")
                return WAITING_RECORD_TTL
        
        # Create the DNS record
        zone_id = context.user_data['zone_id']
        record_type = context.user_data['record_type']
        record_name = context.user_data['record_name']
        record_content = context.user_data['record_content']
        priority = context.user_data.get('record_priority')
        
        cf = await self.get_current_cf_client()
        if not cf:
            await update.message.reply_text("❌ No active tenant selected.")
            return
        
        try:
            record_data = {
                'type': record_type,
                'name': record_name,
                'content': record_content,
                'ttl': ttl
            }
            
            if priority is not None:
                record_data['priority'] = priority
            
            new_record = await asyncio.to_thread(
                cf.dns.records.create,
                zone_id=zone_id,
                **record_data
            )
            
            zones = self.tenants_cache[self.current_tenant_id]['zones']
            zone = zones.get(zone_id)
            full_name = record_name if record_name == '@' else f"{record_name}.{zone.name}"
            
            success_text = (
                "✅ *DNS Record Created Successfully!*\n\n"
                f"🏷️ **Type:** {record_type}\n"
                f"📝 **Name:** `{full_name}`\n"
                f"📄 **Content:** `{record_content}`\n"
                f"⏱️ **TTL:** {ttl} seconds\n"
            )
            
            if priority is not None:
                success_text += f"🔢 **Priority:** {priority}\n"
            
            success_text += f"\n🆔 **Record ID:** `{new_record.id}`"
            
            keyboard = [
                [InlineKeyboardButton("📋 View All Records", callback_data=f"view_records:{zone_id}")],
                [InlineKeyboardButton("➕ Add Another", callback_data=f"add_record:{zone_id}")],
                [InlineKeyboardButton("🔙 Back to Domain", callback_data=f"select_domain:{zone_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                success_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"Error creating DNS record: {e}")
            await update.message.reply_text(
                f"❌ **Error creating DNS record:**\n`{str(e)}`",
                parse_mode='Markdown'
            )
        
        # Clear user data
        context.user_data.clear()
        return ConversationHandler.END

    async def delete_records_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show delete records menu"""
        query = update.callback_query
        await query.answer()
        
        zone_id = query.data.split(':')[1]
        user_id = update.effective_user.id
        
        # Check access
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return
        
        cf = await self.get_current_cf_client()
        if not cf:
            await query.edit_message_text("❌ No active tenant selected.")
            return
        
        try:
            records = await asyncio.to_thread(cf.dns.records.list, zone_id=zone_id)
            
            if not records:
                await query.edit_message_text(
                    "❌ No DNS records found to delete.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back", callback_data=f"select_domain:{zone_id}")
                    ]])
                )
                return
            
            keyboard = []
            for record in records[:20]:  # Limit to 20 records for UI
                keyboard.append([InlineKeyboardButton(
                    f"🗑️ {record.type} - {record.name}",
                    callback_data=f"delete_record:{record.id}:{zone_id}"
                )])
            
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"select_domain:{zone_id}")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            zones = self.tenants_cache[self.current_tenant_id]['zones']
            zone = zones.get(zone_id)
            await query.edit_message_text(
                f"🗑️ *Delete DNS Records for {zone.name}*\n\n"
                "Select a record to delete:\n"
                "⚠️ *This action cannot be undone!*",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"Error fetching records for deletion: {e}")
            await query.edit_message_text(f"❌ Error fetching records: {str(e)}")

    async def delete_record_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and delete a DNS record"""
        query = update.callback_query
        await query.answer()
        
        _, record_id, zone_id = query.data.split(':')
        user_id = update.effective_user.id
        
        # Check access
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return
        
        cf = await self.get_current_cf_client()
        if not cf:
            await query.edit_message_text("❌ No active tenant selected.")
            return
        
        try:
            # Get record details first
            record = await asyncio.to_thread(cf.dns.records.get, zone_id=zone_id, dns_record_id=record_id)
            
            keyboard = [
                [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete:{record_id}:{zone_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"delete_menu:{zone_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"🗑️ *Confirm Deletion*\n\n"
                f"Are you sure you want to delete this record?\n\n"
                f"**Type:** {record.type}\n"
                f"**Name:** `{record.name}`\n"
                f"**Content:** `{record.content}`\n\n"
                f"⚠️ *This action cannot be undone!*",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"Error fetching record for confirmation: {e}")
            await query.edit_message_text(f"❌ Error: {str(e)}")

    async def confirm_delete_record(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Actually delete the DNS record"""
        query = update.callback_query
        await query.answer()
        
        _, record_id, zone_id = query.data.split(':')
        user_id = update.effective_user.id
        
        # Check access
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return
        
        cf = await self.get_current_cf_client()
        if not cf:
            await query.edit_message_text("❌ No active tenant selected.")
            return
        
        try:
            await asyncio.to_thread(cf.dns.records.delete, zone_id=zone_id, dns_record_id=record_id)
            
            keyboard = [
                [InlineKeyboardButton("📋 View Records", callback_data=f"view_records:{zone_id}")],
                [InlineKeyboardButton("🔙 Back to Domain", callback_data=f"select_domain:{zone_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "✅ *DNS Record Deleted Successfully!*\n\n"
                "The record has been removed from your domain.",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"Error deleting DNS record: {e}")
            await query.edit_message_text(f"❌ Error deleting record: {str(e)}")

    async def system_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show system statistics (super admin only)"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        if not await self.is_super_admin(user_id):
            await query.edit_message_text("❌ Access denied. Super admin only.")
            return
        
        try:
            async with self.SessionLocal() as session:
                # Get tenant stats
                result = await session.execute("SELECT COUNT(*) FROM tenants WHERE is_active = 'true'")
                active_tenants = result.fetchone()[0]
                
                result = await session.execute("SELECT COUNT(DISTINCT admin_user_id) FROM tenants WHERE is_active = 'true'")
                unique_admins = result.fetchone()[0]
                
                result = await session.execute("SELECT COUNT(*) FROM domain_groups")
                domain_groups = result.fetchone()[0]
                
                # Get total domains across all tenants
                total_domains = 0
                for tenant_data in self.tenants_cache.values():
                    total_domains += len(tenant_data['domains'])
            
            stats_text = (
                "📊 *System Statistics*\n\n"
                f"🏢 **Active Tenants:** {active_tenants}\n"
                f"👤 **Unique Admins:** {unique_admins}\n"
                f"🌐 **Total Domains:** {total_domains}\n"
                f"📁 **Domain Groups:** {domain_groups}\n"
                f"💾 **Cached Tenants:** {len(self.tenants_cache)}\n\n"
                f"🤖 **Bot Status:** ✅ Running\n"
                f"🔐 **Super Admin:** `{self.super_admin_id}`"
            )
            
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh Stats", callback_data="system_stats")],
                [InlineKeyboardButton("🔙 Back to Settings", callback_data="bot_settings")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                stats_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"Error getting system stats: {e}")
            await query.edit_message_text(f"❌ Error getting stats: {str(e)}")

    async def back_to_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Return to main menu"""
        query = update.callback_query
        await query.answer()
        
        # Reset context and reload start
        context.user_data.clear()
        await self.start_command(update, context)

    async def refresh_cache_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle refresh cache callback"""
        query = update.callback_query
        await query.answer("Refreshing cache...")
        
        user_id = update.effective_user.id
        refreshed_count = 0
        error_count = 0
        
        try:
            if self.current_tenant_id:
                # Refresh current tenant
                await self.refresh_tenant_domains(self.current_tenant_id)
                refreshed_count = 1
            else:
                # Refresh all accessible tenants
                tenants = await self.get_tenants(user_id)
                for tenant in tenants:
                    try:
                        await self.refresh_tenant_domains(tenant.id)
                        refreshed_count += 1
                    except Exception as e:
                        logger.error(f"Error refreshing tenant {tenant.id}: {e}")
                        error_count += 1
            
            status_text = f"✅ *Cache Refreshed!*\n\n"
            status_text += f"🔄 **Refreshed:** {refreshed_count} tenant(s)\n"
            if error_count > 0:
                status_text += f"❌ **Errors:** {error_count} tenant(s)\n"
            
            total_domains = sum(len(data['domains']) for data in self.tenants_cache.values())
            status_text += f"🌐 **Total Domains:** {total_domains}"
            
            await query.edit_message_text(
                status_text,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")
                ]])
            )
            
        except Exception as e:
            logger.error(f"Error refreshing cache: {e}")
            await query.edit_message_text(f"❌ Error refreshing cache: {str(e)}")

    async def cancel_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel current conversation"""
        await update.message.reply_text(
            "❌ Operation cancelled.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")
            ]])
        )
        context.user_data.clear()
        return ConversationHandler.END

    def run(self):
        """Run the bot"""
        application = Application.builder().token(self.telegram_token).build()
        
        # Conversation handler for adding tenants
        add_tenant_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_tenant_start, pattern="^add_tenant$")],
            states={
                WAITING_TENANT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_tenant_name)],
                WAITING_TENANT_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_tenant_description)],
                WAITING_TENANT_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_tenant_admin)],
                WAITING_CF_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_cf_token)],
            },
            fallbacks=[MessageHandler(filters.Regex("^/cancel$"), self.cancel_conversation)]
        )
        
        # Conversation handler for adding DNS records
        add_record_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_record_start, pattern="^add_record:")],
            states={
                WAITING_RECORD_TYPE: [CallbackQueryHandler(self.handle_record_type, pattern="^record_type:")],
                WAITING_RECORD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_record_name)],
                WAITING_RECORD_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_record_content)],
                WAITING_RECORD_PRIORITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_record_priority)],
                WAITING_RECORD_TTL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_record_ttl)],
            },
            fallbacks=[MessageHandler(filters.Regex("^/cancel$"), self.cancel_conversation)]
        )
        
        # Add handlers
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(add_tenant_conv)
        application.add_handler(add_record_conv)
        
        # Callback query handlers
        application.add_handler(CallbackQueryHandler(self.bot_settings_menu, pattern="^bot_settings$"))
        application.add_handler(CallbackQueryHandler(self.manage_all_tenants, pattern="^manage_all_tenants$"))
        application.add_handler(CallbackQueryHandler(self.switch_tenant_menu, pattern="^switch_tenant$"))
        application.add_handler(CallbackQueryHandler(self.set_tenant, pattern="^set_tenant:"))
        application.add_handler(CallbackQueryHandler(self.view_domains, pattern="^view_domains$"))
        application.add_handler(CallbackQueryHandler(self.view_domains, pattern="^manage_dns$"))
        application.add_handler(CallbackQueryHandler(self.select_domain, pattern="^select_domain:"))
        application.add_handler(CallbackQueryHandler(self.view_dns_records, pattern="^view_records:"))
        application.add_handler(CallbackQueryHandler(self.delete_records_menu, pattern="^delete_menu:"))
        application.add_handler(CallbackQueryHandler(self.delete_record_confirm, pattern="^delete_record:"))
        application.add_handler(CallbackQueryHandler(self.confirm_delete_record, pattern="^confirm_delete:"))
        application.add_handler(CallbackQueryHandler(self.system_stats, pattern="^system_stats$"))
        application.add_handler(CallbackQueryHandler(self.back_to_menu, pattern="^back_to_menu$"))
        application.add_handler(CallbackQueryHandler(self.refresh_cache_callback, pattern="^refresh_cache$"))
        
        # Initialize database and run
        asyncio.get_event_loop().run_until_complete(self.init_db())
        application.run_polling()


# Configuration and main execution
async def main():
    """Main function to run the bot"""
    
    # Environment variables
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///dns_bot.db")
    SUPER_ADMIN_ID = os.getenv("SUPER_ADMIN_ID")  # Optional - can be set via bot
    
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is required")
        return
    
    # Create and run bot
    bot = CloudflareDNSBot(
        telegram_token=TELEGRAM_BOT_TOKEN,
        db_url=DATABASE_URL,
        super_admin_id=SUPER_ADMIN_ID
    )
    
    logger.info("Starting Multi-Tenant Cloudflare DNS Manager Bot...")
    logger.info(f"Super Admin ID: {SUPER_ADMIN_ID if SUPER_ADMIN_ID else 'Will be set on first use'}")
    bot.run()


if __name__ == "__main__":
    asyncio.run(main())
