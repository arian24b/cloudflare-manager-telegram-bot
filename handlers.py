"""
CloudflareManager Handlers
Contains all the handler methods for the Telegram bot.
"""

import asyncio
from datetime import datetime

import cloudflare
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from telegram import Update
from telegram.ext import ContextTypes

from models import Tenant, BotConfig
from config import logger


class CloudflareManager:
    """Main bot class containing all handler methods."""

    def __init__(
        self,
        telegram_token: str,
        db_url: str,
        super_admin_id: str | None = None,
    ) -> None:
        self.telegram_token = telegram_token
        self.db_url = db_url
        self.super_admin_id = super_admin_id

        # Database setup - will be initialized in bot.py
        self.engine = None
        self.SessionLocal = None

        # Cache for tenants and their domains
        self.tenants_cache: dict[int, dict] = {}
        self.current_tenant_id: int | None = None

    async def init_db(self) -> None:
        """Initialize database tables."""
        from models import Base

        if not self.engine:
            self.engine = create_async_engine(self.db_url)
            self.SessionLocal = async_sessionmaker(
                bind=self.engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def is_super_admin(self, user_id: int) -> bool:
        """Check if user is super admin."""
        if not self.super_admin_id:
            # If no super admin set, get from config or first-time setup
            if not self.SessionLocal:
                return False
            async with self.SessionLocal() as session:
                result = await session.execute(
                    text("SELECT value FROM bot_config WHERE key = 'super_admin_id'"),
                )
                admin_config = result.fetchone()
                if admin_config:
                    self.super_admin_id = admin_config[0]
                    return str(user_id) == self.super_admin_id
                # First time setup - this user becomes super admin
                await self.set_config("super_admin_id", str(user_id))
                self.super_admin_id = str(user_id)
                return True

        return str(user_id) == self.super_admin_id

    async def is_tenant_admin(self, user_id: int, tenant_id: int | None = None) -> bool:
        """Check if user is admin of a specific tenant or any tenant."""
        if not self.SessionLocal:
            return False
        async with self.SessionLocal() as session:
            if tenant_id:
                # Check specific tenant
                result = await session.execute(
                    text("SELECT admin_user_id FROM tenants WHERE id = :tenant_id AND is_active = 'true'"),
                    {"tenant_id": tenant_id},
                )
                tenant_row = result.fetchone()
                return bool(tenant_row and tenant_row[0] == str(user_id))
            # Check if user is admin of any tenant
            result = await session.execute(
                text("SELECT COUNT(*) FROM tenants WHERE admin_user_id = :user_id AND is_active = 'true'"),
                {"user_id": str(user_id)},
            )
            count_row = result.fetchone()
            return bool(count_row and count_row[0] > 0)

    async def has_access(self, user_id: int, tenant_id: int | None = None) -> bool:
        """Check if user has access (super admin or tenant admin)."""
        if await self.is_super_admin(user_id):
            return True
        if tenant_id:
            return await self.is_tenant_admin(user_id, tenant_id)
        return await self.is_tenant_admin(user_id)

    async def set_config(self, key: str, value: str) -> None:
        """Set configuration value."""
        if not self.SessionLocal:
            return
        async with self.SessionLocal() as session:
            config = BotConfig(key=key, value=value, updated_at=datetime.utcnow())
            await session.merge(config)
            await session.commit()

    async def get_config(self, key: str) -> str | None:
        """Get configuration value."""
        if not self.SessionLocal:
            return None
        async with self.SessionLocal() as session:
            result = await session.execute(
                text("SELECT value FROM bot_config WHERE key = :key"),
                {"key": key},
            )
            config = result.fetchone()
            return config[0] if config else None

    async def get_tenants(self, user_id: int | None = None) -> list[Tenant]:
        """Get tenants (all for super admin, only owned for tenant admin)."""
        if not self.SessionLocal:
            return []
        async with self.SessionLocal() as session:
            stmt = select(Tenant)
            if user_id and not await self.is_super_admin(user_id):
                # Tenant admin - only their tenants
                stmt = stmt.where(Tenant.admin_user_id == str(user_id))
            # Super admin - all tenants
            stmt = stmt.where(Tenant.is_active.is_(True))

            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_tenant_by_id(self, tenant_id: int) -> Tenant | None:
        """Get tenant by ID."""
        if not self.SessionLocal:
            return None
        async with self.SessionLocal() as session:
            stmt = select(Tenant).where(Tenant.id == tenant_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def refresh_tenant_domains(self, tenant_id: int) -> None:
        """Refresh domains cache for a specific tenant."""
        tenant = await self.get_tenant_by_id(tenant_id)
        if not tenant:
            return

        try:
            cf = cloudflare.Cloudflare(api_token=str(tenant.cloudflare_token))
            zones = await asyncio.to_thread(cf.zones.list)

            # Get account ID from zones
            account_id = None
            if hasattr(zones, "result") and zones.result:
                zone = zones.result[0] if isinstance(zones.result, list) else zones.result
                if hasattr(zone, "account") and zone.account:
                    account_id = zone.account.id
            elif isinstance(zones, list) and zones:
                zone = zones[0]
                if hasattr(zone, "account") and zone.account:
                    account_id = zone.account.id

            if not account_id:
                # Fallback: get accounts and use the first one
                try:
                    accounts = await asyncio.to_thread(cf.accounts.list)
                    if hasattr(accounts, "result") and accounts.result:
                        account_id = accounts.result[0].id if isinstance(accounts.result, list) else accounts.result.id
                except Exception as accounts_error:
                    logger.warning(f"Could not fetch accounts: {accounts_error}")

            # Get tunnels for this tenant
            tunnels = []
            if account_id:
                try:
                    # List tunnels for this account
                    tunnels = await asyncio.to_thread(
                        cf.zero_trust.tunnels.list,
                        account_id=account_id,
                    )
                except Exception as tunnel_error:
                    logger.warning(
                        f"Could not fetch tunnels for tenant {tenant.name}: {tunnel_error}",
                    )

            # Store zones and tunnels in cache
            zones_list = []
            tunnels_list = []

            # Handle zones
            if hasattr(zones, "result") and zones.result:
                zones_list = (
                    [zone for zone in zones.result]
                    if hasattr(zones.result, "__iter__") and not isinstance(zones.result, str)
                    else [zones.result]
                )
            elif hasattr(zones, "__iter__") and not isinstance(zones, str):
                zones_list = list(zones)
            else:
                zones_list = [zones] if zones else []

            # Handle tunnels
            if hasattr(tunnels, "result") and tunnels.result:  # type: ignore
                tunnels_list = (
                    [tunnel for tunnel in tunnels.result]
                    if hasattr(tunnels.result, "__iter__") and not isinstance(tunnels.result, str)
                    else [tunnels.result]
                )  # type: ignore
            elif hasattr(tunnels, "__iter__") and not isinstance(tunnels, str):
                tunnels_list = list(tunnels)
            else:
                tunnels_list = [tunnels] if tunnels else []

            zones_dict = {}
            tunnels_dict = {}
            domains_dict = {}

            for zone in zones_list:
                if hasattr(zone, "id") and hasattr(zone, "name"):
                    zones_dict[zone.id] = zone  # type: ignore
                    domains_dict[zone.name] = zone  # type: ignore

            for tunnel in tunnels_list:
                if hasattr(tunnel, "id"):
                    tunnels_dict[tunnel.id] = tunnel  # type: ignore

            self.tenants_cache[tenant_id] = {
                "tenant": tenant,
                "domains": domains_dict,
                "zones": zones_dict,
                "tunnels": tunnels_dict,
                "account_id": account_id,
            }

            zones_count = len(zones_list)
            tunnels_count = len(tunnels_list)
            logger.info(
                f"Refreshed cache for tenant {tenant.name} with {zones_count} domains and {tunnels_count} tunnels",
            )
        except Exception as e:
            logger.exception(f"Error refreshing domains for tenant {tenant_id}: {e}")
            raise

    # ===== TELEGRAM COMMAND HANDLERS =====

    # Command handlers will be added here

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        user = update.effective_user
        message = update.message
        if not user or not message:
            return

        user_id = user.id
        username = user.username or "Unknown"

        # Check if user is super admin
        is_admin = await self.is_super_admin(user_id)

        welcome_text = (
            f"👋 Welcome to CloudflareManager, {username}!\n\n"
            "I'm your multi-tenant Cloudflare management bot.\n\n"
        )

        if is_admin:
            welcome_text += (
                "As a super admin, you can:\n"
                "• Manage tenants and their Cloudflare accounts\n"
                "• View all domains and DNS records\n"
                "• Configure tunnels and security settings\n\n"
                "Use /help to see all available commands."
            )
        else:
            # Check if user has tenant access
            has_tenant_access = await self.is_tenant_admin(user_id)
            if has_tenant_access:
                welcome_text += (
                    "You have access to manage tenant resources.\n"
                    "Use /my_tenants to see your tenants and /help for commands."
                )
            else:
                welcome_text += (
                    "You don't have access to any tenants yet.\n"
                    "Contact your administrator to get access."
                )

        await message.reply_text(welcome_text, parse_mode="HTML")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        user = update.effective_user
        message = update.message
        if not user or not message:
            return

        user_id = user.id
        is_admin = await self.is_super_admin(user_id)

        help_text = (
            "🆘 <b>CloudflareManager Help</b>\n\n"
            "<b>Basic Commands:</b>\n"
            "• /start - Start the bot\n"
            "• /help - Show this help\n"
            "• /status - Bot status\n\n"
        )

        if is_admin:
            help_text += (
                "<b>Admin Commands:</b>\n"
                "• /tenants - List all tenants\n"
                "• /add_tenant - Add new tenant\n"
                "• /domains - View all domains\n"
                "• /tunnels - Manage tunnels\n"
                "• /refresh - Refresh cache\n\n"
                "<b>Tenant Management:</b>\n"
                "• /tenant_info &lt;id&gt; - Get tenant details\n"
                "• /delete_tenant &lt;id&gt; - Remove tenant\n"
            )
        else:
            help_text += (
                "<b>User Commands:</b>\n"
                "• /my_tenants - Your tenants\n"
                "• /domains - Your domains\n"
                "• /tunnels - Your tunnels\n"
            )

        await message.reply_text(help_text, parse_mode="HTML")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command."""
        user = update.effective_user
        message = update.message
        if not user or not message:
            return

        user_id = user.id
        is_admin = await self.is_super_admin(user_id)

        status_text = (
            "📊 <b>CloudflareManager Status</b>\n\n"
            f"🤖 Bot: Running\n"
            f"👤 User ID: {user_id}\n"
            f"👑 Admin: {'Yes' if is_admin else 'No'}\n"
            f"💾 Database: Connected\n"
            f"🌐 Proxy: Configured\n"
            f"📅 Cache: {len(self.tenants_cache)} tenants cached\n\n"
        )

        if is_admin:
            tenants = await self.get_tenants()
            status_text += f"🏢 Total Tenants: {len(tenants)}\n"

        await message.reply_text(status_text, parse_mode="HTML")

    async def tenants_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /tenants command (admin only)."""
        user = update.effective_user
        if not user or not update.message:
            return

        user_id = user.id
        if not await self.is_super_admin(user_id):
            await update.message.reply_text("❌ Access denied. Admin privileges required.")
            return

        tenants = await self.get_tenants()

        if not tenants:
            await update.message.reply_text("📭 No tenants found. Use /add_tenant to create one.")
            return

        response = "🏢 <b>Your Tenants:</b>\n\n"
        for tenant in tenants:
            response += (
                f"🆔 <b>ID:</b> {tenant.id}\n"
                f"📛 <b>Name:</b> {tenant.name}\n"
                f"👤 <b>Admin:</b> {tenant.admin_user_id}\n"
                f"📝 <b>Description:</b> {tenant.description or 'N/A'}\n"
                f"📅 <b>Created:</b> {tenant.created_at.strftime('%Y-%m-%d')}\n\n"
            )

        await update.message.reply_text(response, parse_mode="HTML")

    async def my_tenants_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /my_tenants command."""
        user = update.effective_user
        if not user or not update.message:
            return

        user_id = user.id
        tenants = await self.get_tenants(user_id)

        if not tenants:
            await update.message.reply_text("📭 You don't have any tenants assigned.")
            return

        response = "🏢 <b>Your Tenants:</b>\n\n"
        for tenant in tenants:
            response += (
                f"🆔 <b>ID:</b> {tenant.id}\n"
                f"📛 <b>Name:</b> {tenant.name}\n"
                f"📝 <b>Description:</b> {tenant.description or 'N/A'}\n"
                f"📅 <b>Created:</b> {tenant.created_at.strftime('%Y-%m-%d')}\n\n"
            )

        await update.message.reply_text(response, parse_mode="HTML")

    async def domains_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /domains command."""
        user = update.effective_user
        if not user or not update.message:
            return

        user_id = user.id
        tenants = await self.get_tenants(user_id)

        if not tenants:
            await update.message.reply_text("📭 No tenants found. Contact admin for access.")
            return

        response = "🌐 <b>Your Domains:</b>\n\n"

        for tenant in tenants:
            if tenant.id in self.tenants_cache:  # type: ignore
                cache = self.tenants_cache[tenant.id]  # type: ignore
                domains = cache.get("domains", {})

                if domains:
                    response += f"🏢 <b>{tenant.name}:</b>\n"
                    for domain_name, zone in domains.items():
                        response += f"  • {domain_name}\n"
                    response += "\n"
                else:
                    response += f"🏢 <b>{tenant.name}:</b> No domains cached\n\n"
            else:
                response += f"🏢 <b>{tenant.name}:</b> Cache not loaded (use /refresh)\n\n"

        await update.message.reply_text(response, parse_mode="HTML")

    async def refresh_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /refresh command."""
        user = update.effective_user
        if not user or not update.message:
            return

        user_id = user.id
        tenants = await self.get_tenants(user_id)

        if not tenants:
            await update.message.reply_text("📭 No tenants found.")
            return

        await update.message.reply_text("🔄 Refreshing cache...")

        refreshed_count = 0
        for tenant in tenants:
            try:
                tenant_id = int(tenant.id)  # type: ignore
                await self.refresh_tenant_domains(tenant_id)
                refreshed_count += 1
            except Exception as e:
                logger.error(f"Failed to refresh tenant {tenant.name}: {e}")

        await update.message.reply_text(
            f"✅ <b>Cache Refreshed!</b>\n\n"
            f"Refreshed {refreshed_count} out of {len(tenants)} tenants.",
            parse_mode="HTML"
        )

    async def add_tenant_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /add_tenant command (admin only)."""
        user = update.effective_user
        if not user or not update.message:
            return

        user_id = user.id
        if not await self.is_super_admin(user_id):
            await update.message.reply_text("❌ Access denied. Admin privileges required.")
            return

        # Check if arguments provided
        args = context.args
        if not args or len(args) < 2:
            await update.message.reply_text(
                "📝 <b>Add New Tenant</b>\n\n"
                "Usage: <code>/add_tenant &lt;name&gt; &lt;admin_user_id&gt; [description]</code>\n\n"
                "Example:\n"
                "<code>/add_tenant MyCompany 123456789 Company description</code>\n\n"
                "Parameters:\n"
                "• <b>name</b>: Tenant name (required)\n"
                "• <b>admin_user_id</b>: Telegram user ID of tenant admin (required)\n"
                "• <b>description</b>: Optional description",
                parse_mode="HTML"
            )
            return

        tenant_name = args[0]
        admin_user_id = args[1]
        description = " ".join(args[2:]) if len(args) > 2 else None

        # Validate inputs
        if not admin_user_id.isdigit():
            await update.message.reply_text("❌ Admin user ID must be a number.")
            return

        try:
            # Create tenant
            if not self.SessionLocal:
                await update.message.reply_text("❌ Database not available.")
                return

            async with self.SessionLocal() as session:
                # Check if tenant name already exists
                existing = await session.execute(
                    select(Tenant).where(Tenant.name == tenant_name)
                )
                if existing.scalar_one_or_none():
                    await update.message.reply_text(f"❌ Tenant '{tenant_name}' already exists.")
                    return

                # Create new tenant
                new_tenant = Tenant(
                    name=tenant_name,
                    admin_user_id=admin_user_id,
                    description=description,
                    cloudflare_token="",  # Will be set later
                    is_active=True
                )

                session.add(new_tenant)
                await session.commit()
                await session.refresh(new_tenant)

                await update.message.reply_text(
                    f"✅ <b>Tenant Created Successfully!</b>\n\n"
                    f"🆔 <b>ID:</b> {new_tenant.id}\n"
                    f"📛 <b>Name:</b> {new_tenant.name}\n"
                    f"👤 <b>Admin ID:</b> {new_tenant.admin_user_id}\n"
                    f"📝 <b>Description:</b> {new_tenant.description or 'N/A'}\n\n"
                    f"🔗 <b>Next Step:</b> Use <code>/connect_cf {new_tenant.id}</code> to connect Cloudflare account",
                    parse_mode="HTML"
                )

        except Exception as e:
            logger.error(f"Error creating tenant: {e}")
            await update.message.reply_text("❌ Failed to create tenant. Please try again.")

    async def connect_cf_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /connect_cf command to connect Cloudflare account."""
        user = update.effective_user
        if not user or not update.message:
            return

        user_id = user.id

        # Check if arguments provided
        args = context.args
        if not args:
            await update.message.reply_html(
                "🔗 <b>Connect Cloudflare Account</b>\n\n"
                "Usage: <code>/connect_cf &lt;tenant_id&gt; [api_token]</code>\n\n"
                "Examples:\n"
                "<code>/connect_cf 1 your_cloudflare_api_token_here</code>\n\n"
                "If you don't provide the token, you'll be prompted for it.\n\n"
                "<b>How to get your Cloudflare API Token:</b>\n"
                "1. Go to https://dash.cloudflare.com/profile/api-tokens\n"
                "2. Create a new token with these permissions:\n"
                "   • Zone:Zone:Read\n"
                "   • Zone:DNS:Edit\n"
                "   • Account:Cloudflare Tunnel:Edit\n"
                "   • Account:Cloudflare Tunnel:Read\n"
                "3. Copy the token and use it here"
            )
            return

        tenant_id_str = args[0]

        # Validate tenant ID
        if not tenant_id_str.isdigit():
            await update.message.reply_text("❌ Tenant ID must be a number.")
            return

        tenant_id = int(tenant_id_str)

        # Check permissions
        if not await self.has_access(user_id, tenant_id):
            await update.message.reply_text("❌ Access denied. You don't have permission for this tenant.")
            return

        # Get tenant
        tenant = await self.get_tenant_by_id(tenant_id)
        if not tenant:
            await update.message.reply_text(f"❌ Tenant with ID {tenant_id} not found.")
            return

        # Check if API token provided
        if len(args) > 1:
            api_token = args[1]

            # Validate token
            if not await self.validate_cf_token(api_token):
                await update.message.reply_text(
                    "❌ Invalid Cloudflare API token.\n\n"
                    "Please check:\n"
                    "• Token format is correct\n"
                    "• Token has required permissions\n"
                    "• Token is not expired\n\n"
                    "Use /connect_cf without token to get setup instructions."
                )
                return

            # Save token
            try:
                if not self.SessionLocal:
                    await update.message.reply_text("❌ Database not available.")
                    return

                async with self.SessionLocal() as session:
                    # Update tenant with Cloudflare token
                    stmt = (
                        select(Tenant)
                        .where(Tenant.id == tenant_id)
                    )
                    result = await session.execute(stmt)
                    db_tenant = result.scalar_one()

                    db_tenant.cloudflare_token = api_token  # type: ignore
                    await session.commit()

                    # Clear any cached data for this tenant
                    if tenant_id in self.tenants_cache:
                        del self.tenants_cache[tenant_id]

                    await update.message.reply_text(
                        f"✅ <b>Cloudflare Account Connected!</b>\n\n"
                        f"🏢 <b>Tenant:</b> {tenant.name}\n"
                        f"🔗 <b>Status:</b> Connected\n\n"
                        f"📡 Use <code>/refresh {tenant_id}</code> to load domains and tunnels",
                        parse_mode="HTML"
                    )

            except Exception as e:
                logger.error(f"Error saving Cloudflare token: {e}")
                await update.message.reply_text("❌ Failed to save Cloudflare token. Please try again.")

        else:
            # No token provided - show instructions
            await update.message.reply_text(
                f"🔑 <b>Cloudflare API Token Required</b>\n\n"
                f"🏢 <b>Tenant:</b> {tenant.name} (ID: {tenant_id})\n\n"
                f"<b>To connect your Cloudflare account:</b>\n\n"
                f"1️⃣ Go to: https://dash.cloudflare.com/profile/api-tokens\n\n"
                f"2️⃣ Click <b>'Create Token'</b>\n\n"
                f"3️⃣ Choose <b>'Create Custom Token'</b>\n\n"
                f"4️⃣ Set token name (e.g., 'CloudflareManager-{tenant.name}')\n\n"
                f"5️⃣ Add these permissions:\n"
                f"   • <b>Zone</b> → <b>Zone</b> → <b>Read</b>\n"
                f"   • <b>Zone</b> → <b>DNS</b> → <b>Edit</b>\n"
                f"   • <b>Account</b> → <b>Cloudflare Tunnel</b> → <b>Edit</b>\n"
                f"   • <b>Account</b> → <b>Cloudflare Tunnel</b> → <b>Read</b>\n\n"
                f"6️⃣ Click <b>'Continue to summary'</b>\n\n"
                f"7️⃣ Click <b>'Create Token'</b>\n\n"
                f"8️⃣ <b>Copy the token</b> and run:\n"
                f"<code>/connect_cf {tenant_id} YOUR_TOKEN_HERE</code>\n\n"
                f"⚠️ <b>Important:</b> Keep your token secure and don't share it!",
                parse_mode="HTML"
            )

    async def validate_cf_token(self, api_token: str) -> bool:
        """Validate Cloudflare API token by testing it."""
        try:
            cf = cloudflare.Cloudflare(api_token=api_token)
            # Try to list zones to validate token
            await asyncio.to_thread(cf.zones.list)
            return True
        except Exception as e:
            logger.warning(f"Cloudflare token validation failed: {e}")
            return False

    async def tenant_info_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /tenant_info command."""
        user = update.effective_user
        if not user or not update.message:
            return

        user_id = user.id
        args = context.args

        if not args or not args[0].isdigit():
            await update.message.reply_text("Usage: /tenant_info <tenant_id>")
            return

        tenant_id = int(args[0])

        # Check permissions
        if not await self.has_access(user_id, tenant_id):
            await update.message.reply_text("❌ Access denied. You don't have permission for this tenant.")
            return

        tenant = await self.get_tenant_by_id(tenant_id)
        if not tenant:
            await update.message.reply_text(f"❌ Tenant with ID {tenant_id} not found.")
            return

        # Get additional info
        has_token = bool(tenant.cloudflare_token)
        in_cache = tenant_id in self.tenants_cache

        response = (
            f"🏢 <b>Tenant Information</b>\n\n"
            f"🆔 <b>ID:</b> {tenant.id}\n"
            f"📛 <b>Name:</b> {tenant.name}\n"
            f"👤 <b>Admin ID:</b> {tenant.admin_user_id}\n"
            f"📝 <b>Description:</b> {tenant.description or 'N/A'}\n"
            f"🔗 <b>Cloudflare:</b> {'✅ Connected' if has_token else '❌ Not connected'}\n"
            f"💾 <b>Cache:</b> {'✅ Loaded' if in_cache else '❌ Not loaded'}\n"
            f"📅 <b>Created:</b> {tenant.created_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"🔄 <b>Active:</b> {'✅ Yes' if tenant.is_active else '❌ No'}\n\n"  # type: ignore
        )

        if in_cache:
            cache = self.tenants_cache[tenant_id]
            domains = cache.get("domains", {})
            tunnels = cache.get("tunnels", {})
            response += f"🌐 <b>Domains:</b> {len(domains)}\n"
            response += f"🚇 <b>Tunnels:</b> {len(tunnels)}\n"

        await update.message.reply_text(response, parse_mode="HTML")
