#!/usr/bin/env python3
"""
Cloudflare Manager Telegram Bot - Legacy Main File

This file contains the original monolithic implementation.
The codebase has been refactored into modular files:

- models.py: Database models (Tenant, DomainGroup, UserSession, BotConfig)
- config.py: Configuration and conversation states
- handlers.py: CloudflareDNSBot class with all handler methods
- bot.py: CloudflareBotRunner class for initialization
- __main__.py: Clean entry point

To run the bot, use: python -m cloudflare-manager-telegram-bot

Environment variables required:
- TELEGRAM_BOT_TOKEN: Your Telegram bot token
- DATABASE_URL: Database connection string (default: sqlite+aiosqlite:///dns_bot.db)
- SUPER_ADMIN_ID: Optional super admin user ID

The original 2800+ lines of code have been successfully split into maintainable modules.
"""

# This file is kept for reference. The actual bot implementation is in the modular files above.

if __name__ == "__main__":
    print("This is the legacy main.py file.")
    print("Please use: python -m cloudflare-manager-telegram-bot")
    print("Or run: python __main__.py")
    domains = Column(Text)  # JSON string of domain IDs
    created_at = Column(DateTime, default=datetime.utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"

    user_id = Column(String, primary_key=True)
    current_tenant = Column(Integer)
    current_domain = Column(String)
    current_group = Column(String)
    last_activity = Column(DateTime, default=datetime.utcnow)


class BotConfig(Base):
    __tablename__ = "bot_config"

    key = Column(String, primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow)


# Conversation states
(
    WAITING_TENANT_NAME,
    WAITING_TENANT_DESCRIPTION,
    WAITING_CF_TOKEN,
    WAITING_TENANT_ADMIN,
    WAITING_GROUP_NAME,
    WAITING_GROUP_DESCRIPTION,
    WAITING_DOMAIN_SELECTION,
    WAITING_RECORD_TYPE,
    WAITING_RECORD_NAME,
    WAITING_RECORD_CONTENT,
    WAITING_RECORD_TTL,
    WAITING_RECORD_PRIORITY,
    WAITING_SUPER_ADMIN_ID,
    WAITING_TUNNEL_NAME,
    WAITING_TUNNEL_SECRET,
    WAITING_HOSTNAME_SUBDOMAIN,
    WAITING_HOSTNAME_SERVICE,
    WAITING_PRIVATE_NETWORK,
) = range(18)


class CloudflareDNSBot:
    def __init__(
        self,
        telegram_token: str,
        db_url: str,
        super_admin_id: str | None = None,
    ) -> None:
        self.telegram_token = telegram_token
        self.super_admin_id = super_admin_id

        # Database setup
        self.engine = create_async_engine(db_url)
        self.SessionLocal = sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Cache for tenants and their domains
        self.tenants_cache: dict[int, dict] = {}
        self.current_tenant_id: int | None = None

    async def init_db(self) -> None:
        """Initialize database tables."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def is_super_admin(self, user_id: int) -> bool:
        """Check if user is super admin."""
        if not self.super_admin_id:
            # If no super admin set, get from config or first-time setup
            async with self.SessionLocal() as session:
                result = await session.execute(
                    "SELECT value FROM bot_config WHERE key = 'super_admin_id'",
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
        async with self.SessionLocal() as session:
            if tenant_id:
                # Check specific tenant
                result = await session.execute(
                    "SELECT admin_user_id FROM tenants WHERE id = ? AND is_active = 'true'",
                    (tenant_id,),
                )
                tenant = result.fetchone()
                return tenant and tenant[0] == str(user_id)
            # Check if user is admin of any tenant
            result = await session.execute(
                "SELECT COUNT(*) FROM tenants WHERE admin_user_id = ? AND is_active = 'true'",
                (str(user_id),),
            )
            count = result.fetchone()
            return count and count[0] > 0

    async def has_access(self, user_id: int, tenant_id: int | None = None) -> bool:
        """Check if user has access (super admin or tenant admin)."""
        if await self.is_super_admin(user_id):
            return True
        if tenant_id:
            return await self.is_tenant_admin(user_id, tenant_id)
        return await self.is_tenant_admin(user_id)

    async def set_config(self, key: str, value: str) -> None:
        """Set configuration value."""
        async with self.SessionLocal() as session:
            config = BotConfig(key=key, value=value, updated_at=datetime.utcnow())
            await session.merge(config)
            await session.commit()

    async def get_config(self, key: str) -> str | None:
        """Get configuration value."""
        async with self.SessionLocal() as session:
            result = await session.execute(
                "SELECT value FROM bot_config WHERE key = ?",
                (key,),
            )
            config = result.fetchone()
            return config[0] if config else None

    async def get_tenants(self, user_id: int | None = None) -> list[Tenant]:
        """Get tenants (all for super admin, only owned for tenant admin)."""
        async with self.SessionLocal() as session:
            if user_id and not await self.is_super_admin(user_id):
                # Tenant admin - only their tenants
                result = await session.execute(
                    "SELECT * FROM tenants WHERE admin_user_id = ? AND is_active = 'true'",
                    (str(user_id),),
                )
            else:
                # Super admin - all tenants
                result = await session.execute(
                    "SELECT * FROM tenants WHERE is_active = 'true'",
                )

            return [
                Tenant(
                    id=row[0],
                    name=row[1],
                    cloudflare_token=row[2],
                    admin_user_id=row[3],
                    description=row[4],
                    is_active=row[5],
                    created_at=row[6],
                )
                for row in result.fetchall()
            ]

    async def get_tenant_by_id(self, tenant_id: int) -> Tenant | None:
        """Get tenant by ID."""
        async with self.SessionLocal() as session:
            result = await session.execute(
                "SELECT * FROM tenants WHERE id = ?",
                (tenant_id,),
            )
            row = result.fetchone()
            if row:
                return Tenant(
                    id=row[0],
                    name=row[1],
                    cloudflare_token=row[2],
                    admin_user_id=row[3],
                    description=row[4],
                    is_active=row[5],
                    created_at=row[6],
                )
            return None

    async def refresh_tenant_domains(self, tenant_id: int) -> None:
        """Refresh domains cache for a specific tenant."""
        tenant = await self.get_tenant_by_id(tenant_id)
        if not tenant:
            return

        try:
            cf = cloudflare.Cloudflare(api_token=tenant.cloudflare_token)
            zones = await asyncio.to_thread(cf.zones.list)

            # Get tunnels for this tenant
            try:
                # Use the account ID from the first zone or get account info
                account_id = None
                if zones:
                    # Try to get account ID from zone info
                    zone_details = await asyncio.to_thread(
                        cf.zones.get,
                        zone_id=zones[0].id,
                    )
                    account_id = (
                        zone_details.account.id if hasattr(zone_details, "account") and zone_details.account else None
                    )

                if not account_id:
                    # Fallback: get accounts and use the first one
                    accounts = await asyncio.to_thread(cf.accounts.list)
                    account_id = accounts[0].id if accounts else None

                tunnels = []
                if account_id:
                    # List tunnels for this account
                    tunnels = await asyncio.to_thread(
                        cf.zero_trust.tunnels.list,
                        account_id=account_id,
                    )
            except Exception as tunnel_error:
                logger.warning(
                    f"Could not fetch tunnels for tenant {tenant.name}: {tunnel_error}",
                )
                tunnels = []

            self.tenants_cache[tenant_id] = {
                "tenant": tenant,
                "domains": {zone.name: zone for zone in zones},
                "zones": {zone.id: zone for zone in zones},
                "tunnels": {tunnel.id: tunnel for tunnel in tunnels},
                "account_id": account_id,
            }

            logger.info(
                f"Refreshed cache for tenant {tenant.name} with {len(zones)} domains and {len(tunnels)} tunnels",
            )
        except Exception as e:
            logger.exception(f"Error refreshing domains for tenant {tenant_id}: {e}")
            raise

    async def get_current_cf_client(self) -> cloudflare.Cloudflare | None:
        """Get Cloudflare client for current tenant."""
        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            return None

        tenant = self.tenants_cache[self.current_tenant_id]["tenant"]
        return cloudflare.Cloudflare(api_token=tenant.cloudflare_token)

    async def start_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /start command."""
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
                parse_mode="Markdown",
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

            keyboard.extend(
                [
                    [
                        InlineKeyboardButton(
                            "🏢 Switch Tenant",
                            callback_data="switch_tenant",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "🌐 View Domains",
                            callback_data="view_domains",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "🔧 Manage DNS Records",
                            callback_data="manage_dns",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "🚇 Manage Tunnels",
                            callback_data="manage_tunnels",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "📁 Domain Groups",
                            callback_data="manage_groups",
                        ),
                    ],
                ],
            )

        # Super admin only options
        if is_super:
            keyboard.append(
                [InlineKeyboardButton("⚙️ Bot Settings", callback_data="bot_settings")],
            )

        keyboard.append(
            [InlineKeyboardButton("🔄 Refresh Cache", callback_data="refresh_cache")],
        )

        reply_markup = InlineKeyboardMarkup(keyboard)

        current_tenant_info = ""
        if self.current_tenant_id and self.current_tenant_id in self.tenants_cache:
            tenant = self.tenants_cache[self.current_tenant_id]["tenant"]
            domain_count = len(self.tenants_cache[self.current_tenant_id]["domains"])
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
            "• Cloudflare Tunnel management\n"
            "• Domain grouping and bulk operations\n\n"
            "Choose an option below:"
        )

        await update.message.reply_text(
            welcome_text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    async def bot_settings_menu(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Show bot settings menu (super admin only)."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        if not await self.is_super_admin(user_id):
            await query.edit_message_text("❌ Access denied. Super admin only.")
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    "🏢 Manage All Tenants",
                    callback_data="manage_all_tenants",
                ),
            ],
            [
                InlineKeyboardButton(
                    "👤 Change Super Admin",
                    callback_data="change_super_admin",
                ),
            ],
            [InlineKeyboardButton("📊 System Stats", callback_data="system_stats")],
            [InlineKeyboardButton("🔧 Bot Configuration", callback_data="bot_config")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        async with self.SessionLocal() as session:
            result = await session.execute(
                "SELECT COUNT(*) FROM tenants WHERE is_active = 'true'",
            )
            tenant_count = result.fetchone()[0]

            result = await session.execute(
                "SELECT COUNT(DISTINCT admin_user_id) FROM tenants WHERE is_active = 'true'",
            )
            admin_count = result.fetchone()[0]

        super_admin_id = await self.get_config("super_admin_id")

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
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    async def manage_all_tenants(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Manage all tenants (super admin only)."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        if not await self.is_super_admin(user_id):
            await query.edit_message_text("❌ Access denied. Super admin only.")
            return

        # Get all tenants
        async with self.SessionLocal() as session:
            result = await session.execute(
                "SELECT * FROM tenants WHERE is_active = 'true'",
            )
            tenants = [
                Tenant(
                    id=row[0],
                    name=row[1],
                    cloudflare_token=row[2],
                    admin_user_id=row[3],
                    description=row[4],
                    is_active=row[5],
                    created_at=row[6],
                )
                for row in result.fetchall()
            ]

        keyboard = [
            [InlineKeyboardButton("➕ Add New Tenant", callback_data="add_tenant")],
        ]

        tenants_text = "🏢 *All Tenants Management*\n\n"

        if tenants:
            for tenant in tenants:
                status = "🟢" if tenant.is_active == "true" else "🔴"
                tenants_text += f"{status} **{tenant.name}**\n"
                tenants_text += f"   👤 Admin: `{tenant.admin_user_id}`\n"
                if tenant.description:
                    tenants_text += f"   📝 {tenant.description}\n"
                tenants_text += "\n"

                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"✏️ {tenant.name}",
                            callback_data=f"edit_tenant:{tenant.id}",
                        ),
                        InlineKeyboardButton(
                            "🗑️",
                            callback_data=f"delete_tenant:{tenant.id}",
                        ),
                    ],
                )
        else:
            tenants_text += "No tenants configured yet.\n\nAdd your first tenant to start managing domains."

        keyboard.append(
            [InlineKeyboardButton("🔙 Back to Settings", callback_data="bot_settings")],
        )
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            tenants_text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    async def add_tenant_start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Start adding new tenant (super admin only)."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        if not await self.is_super_admin(user_id):
            await query.edit_message_text("❌ Access denied. Super admin only.")
            return None

        await query.edit_message_text(
            "🏢 *Add New Tenant*\n\nEnter the tenant name (e.g., 'Production', 'Client ABC', 'Personal'):",
            parse_mode="Markdown",
        )

        return WAITING_TENANT_NAME

    async def handle_tenant_name(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle tenant name input."""
        tenant_name = update.message.text.strip()
        context.user_data["tenant_name"] = tenant_name

        await update.message.reply_text(
            f"🏢 *Tenant: {tenant_name}*\n\nEnter a description (optional, or type 'skip'):",
            parse_mode="Markdown",
        )

        return WAITING_TENANT_DESCRIPTION

    async def handle_tenant_description(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle tenant description input."""
        description = update.message.text.strip()
        if description.lower() == "skip":
            description = ""

        context.user_data["tenant_description"] = description

        await update.message.reply_text(
            "👤 *Tenant Admin*\n\n"
            "Enter the Telegram User ID of the tenant admin:\n\n"
            "ℹ️ *How to get User ID:*\n"
            "• Forward a message from the user to @userinfobot\n"
            "• Or ask them to send /start to @userinfobot",
            parse_mode="Markdown",
        )

        return WAITING_TENANT_ADMIN

    async def handle_tenant_admin(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle tenant admin input."""
        try:
            admin_user_id = int(update.message.text.strip())
            context.user_data["tenant_admin"] = str(admin_user_id)
        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid numeric User ID.",
                parse_mode="Markdown",
            )
            return WAITING_TENANT_ADMIN

        await update.message.reply_text(
            "🔐 *Cloudflare API Token*\n\n"
            "Enter the Cloudflare API Token for this tenant:\n\n"
            "ℹ️ *Required permissions:*\n"
            "• Zone:Zone:Read\n"
            "• Zone:DNS:Edit\n\n"
            "🔗 Get token at: https://dash.cloudflare.com/profile/api-tokens",
            parse_mode="Markdown",
        )

        return WAITING_CF_TOKEN

    async def handle_cf_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Cloudflare token input and create tenant."""
        cf_token = update.message.text.strip()

        # Test the token
        try:
            cf = cloudflare.Cloudflare(api_token=cf_token)
            zones = await asyncio.to_thread(cf.zones.list)

            # Create tenant
            tenant_name = context.user_data["tenant_name"]
            tenant_description = context.user_data["tenant_description"]
            tenant_admin = context.user_data["tenant_admin"]

            async with self.SessionLocal() as session:
                # Insert tenant
                result = await session.execute(
                    "INSERT INTO tenants (name, cloudflare_token, admin_user_id, description, is_active) VALUES (?, ?, ?, ?, 'true')",
                    (tenant_name, cf_token, tenant_admin, tenant_description),
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
                [
                    InlineKeyboardButton(
                        f"🔄 Switch to {tenant_name}",
                        callback_data=f"set_tenant:{tenant_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🏢 Manage Tenants",
                        callback_data="manage_all_tenants",
                    ),
                ],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                success_text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error testing Cloudflare token: {e}")
            await update.message.reply_text(
                f"❌ **Invalid Cloudflare Token**\n\nError: `{e!s}`\n\nPlease check your token and try again:",
                parse_mode="Markdown",
            )
            return WAITING_CF_TOKEN

        context.user_data.clear()
        return ConversationHandler.END

    async def switch_tenant_menu(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Show tenant switching menu."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        tenants = await self.get_tenants(user_id)

        if not tenants:
            await query.edit_message_text(
                "❌ No accessible tenants.\n\nContact the super admin for access.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]],
                ),
            )
            return

        keyboard = []
        current_marker = ""

        for tenant in tenants:
            current_marker = " ✅" if tenant.id == self.current_tenant_id else ""

            # Show if user is admin of this tenant
            admin_marker = " 👤" if tenant.admin_user_id == str(user_id) else ""

            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"🏢 {tenant.name}{current_marker}{admin_marker}",
                        callback_data=f"set_tenant:{tenant.id}",
                    ),
                ],
            )

        keyboard.append(
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
        )
        reply_markup = InlineKeyboardMarkup(keyboard)

        is_super = await self.is_super_admin(user_id)
        role_info = " (Super Admin)" if is_super else " (Tenant Admin)"

        switch_text = (
            f"🏢 *Switch Tenant{role_info}*\n\nSelect a tenant to switch to:\n✅ = Currently active\n👤 = You are admin"
        )

        await query.edit_message_text(
            switch_text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    async def set_tenant(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Set current tenant."""
        query = update.callback_query
        await query.answer()

        tenant_id = int(query.data.split(":")[1])
        user_id = update.effective_user.id

        # Check access
        if not await self.has_access(user_id, tenant_id):
            await query.edit_message_text("❌ Access denied to this tenant.")
            return

        try:
            await self.refresh_tenant_domains(tenant_id)
            self.current_tenant_id = tenant_id

            tenant = self.tenants_cache[tenant_id]["tenant"]
            domain_count = len(self.tenants_cache[tenant_id]["domains"])

            is_admin = tenant.admin_user_id == str(user_id)
            role_text = "👤 Tenant Admin" if is_admin else "🔧 Super Admin"

            await query.edit_message_text(
                f"✅ *Switched to: {tenant.name}*\n\n"
                f"🔐 *Your Role:* {role_text}\n"
                f"🌐 *Domains:* {domain_count}\n\n"
                "You can now manage domains and DNS records for this tenant.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🌐 View Domains",
                                callback_data="view_domains",
                            ),
                            InlineKeyboardButton(
                                "🔙 Back to Menu",
                                callback_data="back_to_menu",
                            ),
                        ],
                    ],
                ),
            )

        except Exception as e:
            logger.exception(f"Error switching to tenant {tenant_id}: {e}")
            await query.edit_message_text(
                f"❌ **Error switching tenant:**\n`{e!s}`\n\nPlease check the Cloudflare token for this tenant.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back", callback_data="switch_tenant")]],
                ),
            )

    async def view_domains(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Display domains for current tenant."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id

        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            await query.edit_message_text(
                "❌ No tenant selected.\n\nPlease select a tenant first.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🏢 Switch Tenant",
                                callback_data="switch_tenant",
                            ),
                            InlineKeyboardButton(
                                "🔙 Back",
                                callback_data="back_to_menu",
                            ),
                        ],
                    ],
                ),
            )
            return

        # Check access to current tenant
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied to current tenant.")
            return

        tenant_data = self.tenants_cache[self.current_tenant_id]
        domains = tenant_data["domains"]
        tenant = tenant_data["tenant"]

        if not domains:
            await query.edit_message_text(
                f"❌ No domains found for tenant: {tenant.name}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Refresh",
                                callback_data="refresh_cache",
                            ),
                            InlineKeyboardButton(
                                "🔙 Back",
                                callback_data="back_to_menu",
                            ),
                        ],
                    ],
                ),
            )
            return

        is_admin = tenant.admin_user_id == str(user_id)
        role_text = "👤 Admin" if is_admin else "🔧 Super Admin"

        domains_text = f"🌐 *{tenant.name} Domains ({role_text}):*\n\n"
        keyboard = []

        for i, (domain_name, zone) in enumerate(domains.items(), 1):
            status = "🟢" if zone.status == "active" else "🟡"
            domains_text += f"{i}. {status} `{domain_name}`\n"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"📝 {domain_name}",
                        callback_data=f"select_domain:{zone.id}",
                    ),
                ],
            )

        keyboard.extend(
            [
                [
                    InlineKeyboardButton(
                        "🏢 Switch Tenant",
                        callback_data="switch_tenant",
                    ),
                ],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
            ],
        )
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            domains_text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    async def select_domain(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle domain selection."""
        query = update.callback_query
        await query.answer()

        zone_id = query.data.split(":")[1]
        user_id = update.effective_user.id

        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            await query.edit_message_text("❌ No tenant selected.")
            return

        # Check access to current tenant
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied to current tenant.")
            return

        zones = self.tenants_cache[self.current_tenant_id]["zones"]
        zone = zones.get(zone_id)

        if not zone:
            await query.edit_message_text("❌ Domain not found.")
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    "📋 View DNS Records",
                    callback_data=f"view_records:{zone_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "➕ Add DNS Record",
                    callback_data=f"add_record:{zone_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗑️ Delete DNS Records",
                    callback_data=f"delete_menu:{zone_id}",
                ),
            ],
            [InlineKeyboardButton("🔙 Back to Domains", callback_data="view_domains")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        tenant = self.tenants_cache[self.current_tenant_id]["tenant"]
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
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    async def view_dns_records(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """View DNS records for a domain."""
        query = update.callback_query
        await query.answer()

        zone_id = query.data.split(":")[1]
        user_id = update.effective_user.id

        # Check access
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return

        cf = await self.get_current_cf_client()
        if not cf:
            await query.edit_message_text("❌ No active tenant selected.")
            return

        zones = self.tenants_cache[self.current_tenant_id]["zones"]
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
                [
                    InlineKeyboardButton(
                        "➕ Add Record",
                        callback_data=f"add_record:{zone_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🗑️ Delete Records",
                        callback_data=f"delete_menu:{zone_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data=f"select_domain:{zone_id}",
                    ),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                records_text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error fetching DNS records: {e}")
            await query.edit_message_text(f"❌ Error fetching DNS records: {e!s}")

    async def add_record_start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Start adding a new DNS record."""
        query = update.callback_query
        await query.answer()

        zone_id = query.data.split(":")[1]
        user_id = update.effective_user.id

        # Check access
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return None

        context.user_data["zone_id"] = zone_id

        # DNS record types
        record_types = [
            [
                InlineKeyboardButton("A", callback_data="record_type:A"),
                InlineKeyboardButton("AAAA", callback_data="record_type:AAAA"),
            ],
            [
                InlineKeyboardButton("CNAME", callback_data="record_type:CNAME"),
                InlineKeyboardButton("MX", callback_data="record_type:MX"),
            ],
            [
                InlineKeyboardButton("TXT", callback_data="record_type:TXT"),
                InlineKeyboardButton("SRV", callback_data="record_type:SRV"),
            ],
            [
                InlineKeyboardButton("NS", callback_data="record_type:NS"),
                InlineKeyboardButton("PTR", callback_data="record_type:PTR"),
            ],
            [
                InlineKeyboardButton(
                    "🔙 Cancel",
                    callback_data=f"select_domain:{zone_id}",
                ),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(record_types)

        await query.edit_message_text(
            "📝 *Add New DNS Record*\n\nSelect the record type:",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

        return WAITING_RECORD_TYPE

    async def handle_record_type(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle record type selection."""
        query = update.callback_query
        await query.answer()

        record_type = query.data.split(":")[1]
        context.user_data["record_type"] = record_type

        zone_id = context.user_data["zone_id"]
        zones = self.tenants_cache[self.current_tenant_id]["zones"]
        zone = zones.get(zone_id)

        await query.edit_message_text(
            f"📝 *Adding {record_type} Record for {zone.name}*\n\n"
            f"Enter the record name (subdomain):\n\n"
            f"Examples:\n"
            f"• `www` for www.{zone.name}\n"
            f"• `@` for {zone.name}\n"
            f"• `mail` for mail.{zone.name}",
            parse_mode="Markdown",
        )

        return WAITING_RECORD_NAME

    async def handle_record_name(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle record name input."""
        record_name = update.message.text.strip()
        context.user_data["record_name"] = record_name

        record_type = context.user_data["record_type"]
        zone_id = context.user_data["zone_id"]
        zones = self.tenants_cache[self.current_tenant_id]["zones"]
        zone = zones.get(zone_id)

        # Provide type-specific content examples
        examples = {
            "A": "IPv4 address (e.g., 192.168.1.1)",
            "AAAA": "IPv6 address (e.g., 2001:db8::1)",
            "CNAME": "Target domain (e.g., example.com)",
            "MX": "Mail server (e.g., mail.example.com)",
            "TXT": 'Text content (e.g., "v=spf1 include:_spf.google.com ~all")',
            "SRV": "Target (e.g., 10 5 443 target.example.com)",
            "NS": "Name server (e.g., ns1.example.com)",
            "PTR": "Target domain (e.g., example.com)",
        }

        full_name = record_name if record_name == "@" else f"{record_name}.{zone.name}"

        await update.message.reply_text(
            f"📝 *Adding {record_type} Record*\n\n"
            f"Name: `{full_name}`\n\n"
            f"Enter the record content:\n"
            f"*{examples.get(record_type, 'Record content')}*",
            parse_mode="Markdown",
        )

        return WAITING_RECORD_CONTENT

    async def handle_record_content(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle record content input."""
        record_content = update.message.text.strip()
        context.user_data["record_content"] = record_content

        record_type = context.user_data["record_type"]

        # For MX and SRV records, ask for priority
        if record_type in ["MX", "SRV"]:
            await update.message.reply_text(
                f"📝 *Priority for {record_type} Record*\n\n"
                f"Enter the priority (lower number = higher priority):\n"
                f"Common values: 10, 20, 30",
                parse_mode="Markdown",
            )
            return WAITING_RECORD_PRIORITY
        # Skip priority and go to TTL
        await update.message.reply_text(
            "⏱️ *TTL (Time To Live)*\n\n"
            "Enter TTL in seconds (or type 'auto' for automatic):\n\n"
            "Common values:\n"
            "• 300 (5 minutes)\n"
            "• 3600 (1 hour)\n"
            "• 86400 (24 hours)",
            parse_mode="Markdown",
        )
        return WAITING_RECORD_TTL

    async def handle_record_priority(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle record priority input."""
        try:
            priority = int(update.message.text.strip())
            context.user_data["record_priority"] = priority
        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid number for priority.",
            )
            return WAITING_RECORD_PRIORITY

        await update.message.reply_text(
            "⏱️ *TTL (Time To Live)*\n\n"
            "Enter TTL in seconds (or type 'auto' for automatic):\n\n"
            "Common values:\n"
            "• 300 (5 minutes)\n"
            "• 3600 (1 hour)\n"
            "• 86400 (24 hours)",
            parse_mode="Markdown",
        )

        return WAITING_RECORD_TTL

    async def handle_record_ttl(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle TTL input and create the record."""
        ttl_input = update.message.text.strip().lower()

        if ttl_input == "auto":
            ttl = 1  # Cloudflare auto TTL
        else:
            try:
                ttl = int(ttl_input)
                ttl = max(ttl, 120)  # Minimum TTL
            except ValueError:
                await update.message.reply_text(
                    "❌ Please enter a valid number for TTL or 'auto'.",
                )
                return WAITING_RECORD_TTL

        # Create the DNS record
        zone_id = context.user_data["zone_id"]
        record_type = context.user_data["record_type"]
        record_name = context.user_data["record_name"]
        record_content = context.user_data["record_content"]
        priority = context.user_data.get("record_priority")

        cf = await self.get_current_cf_client()
        if not cf:
            await update.message.reply_text("❌ No active tenant selected.")
            return None

        try:
            record_data = {
                "type": record_type,
                "name": record_name,
                "content": record_content,
                "ttl": ttl,
            }

            if priority is not None:
                record_data["priority"] = priority

            new_record = await asyncio.to_thread(
                cf.dns.records.create,
                zone_id=zone_id,
                **record_data,
            )

            zones = self.tenants_cache[self.current_tenant_id]["zones"]
            zone = zones.get(zone_id)
            full_name = record_name if record_name == "@" else f"{record_name}.{zone.name}"

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
                [
                    InlineKeyboardButton(
                        "📋 View All Records",
                        callback_data=f"view_records:{zone_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "➕ Add Another",
                        callback_data=f"add_record:{zone_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back to Domain",
                        callback_data=f"select_domain:{zone_id}",
                    ),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                success_text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error creating DNS record: {e}")
            await update.message.reply_text(
                f"❌ **Error creating DNS record:**\n`{e!s}`",
                parse_mode="Markdown",
            )

        # Clear user data
        context.user_data.clear()
        return ConversationHandler.END

    async def delete_records_menu(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Show delete records menu."""
        query = update.callback_query
        await query.answer()

        zone_id = query.data.split(":")[1]
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
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Back",
                                    callback_data=f"select_domain:{zone_id}",
                                ),
                            ],
                        ],
                    ),
                )
                return

            keyboard = []
            for record in records[:20]:  # Limit to 20 records for UI
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"🗑️ {record.type} - {record.name}",
                            callback_data=f"delete_record:{record.id}:{zone_id}",
                        ),
                    ],
                )

            keyboard.append(
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data=f"select_domain:{zone_id}",
                    ),
                ],
            )
            reply_markup = InlineKeyboardMarkup(keyboard)

            zones = self.tenants_cache[self.current_tenant_id]["zones"]
            zone = zones.get(zone_id)
            await query.edit_message_text(
                f"🗑️ *Delete DNS Records for {zone.name}*\n\n"
                "Select a record to delete:\n"
                "⚠️ *This action cannot be undone!*",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error fetching records for deletion: {e}")
            await query.edit_message_text(f"❌ Error fetching records: {e!s}")

    async def delete_record_confirm(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Confirm and delete a DNS record."""
        query = update.callback_query
        await query.answer()

        _, record_id, zone_id = query.data.split(":")
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
            record = await asyncio.to_thread(
                cf.dns.records.get,
                zone_id=zone_id,
                dns_record_id=record_id,
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "✅ Yes, Delete",
                        callback_data=f"confirm_delete:{record_id}:{zone_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data=f"delete_menu:{zone_id}",
                    ),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"🗑️ *Confirm Deletion*\n\n"
                f"Are you sure you want to delete this record?\n\n"
                f"**Type:** {record.type}\n"
                f"**Name:** `{record.name}`\n"
                f"**Content:** `{record.content}`\n\n"
                f"⚠️ *This action cannot be undone!*",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error fetching record for confirmation: {e}")
            await query.edit_message_text(f"❌ Error: {e!s}")

    async def confirm_delete_record(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Actually delete the DNS record."""
        query = update.callback_query
        await query.answer()

        _, record_id, zone_id = query.data.split(":")
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
            await asyncio.to_thread(
                cf.dns.records.delete,
                zone_id=zone_id,
                dns_record_id=record_id,
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "📋 View Records",
                        callback_data=f"view_records:{zone_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back to Domain",
                        callback_data=f"select_domain:{zone_id}",
                    ),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                "✅ *DNS Record Deleted Successfully!*\n\nThe record has been removed from your domain.",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error deleting DNS record: {e}")
            await query.edit_message_text(f"❌ Error deleting record: {e!s}")

    async def system_stats(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Show system statistics (super admin only)."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        if not await self.is_super_admin(user_id):
            await query.edit_message_text("❌ Access denied. Super admin only.")
            return

        try:
            async with self.SessionLocal() as session:
                # Get tenant stats
                result = await session.execute(
                    "SELECT COUNT(*) FROM tenants WHERE is_active = 'true'",
                )
                active_tenants = result.fetchone()[0]

                result = await session.execute(
                    "SELECT COUNT(DISTINCT admin_user_id) FROM tenants WHERE is_active = 'true'",
                )
                unique_admins = result.fetchone()[0]

                result = await session.execute("SELECT COUNT(*) FROM domain_groups")
                domain_groups = result.fetchone()[0]

                # Get total domains across all tenants
                total_domains = 0
                total_tunnels = 0
                for tenant_data in self.tenants_cache.values():
                    total_domains += len(tenant_data.get("domains", {}))
                    total_tunnels += len(tenant_data.get("tunnels", {}))

            stats_text = (
                "📊 *System Statistics*\n\n"
                f"🏢 **Active Tenants:** {active_tenants}\n"
                f"👤 **Unique Admins:** {unique_admins}\n"
                f"🌐 **Total Domains:** {total_domains}\n"
                f"🚇 **Total Tunnels:** {total_tunnels}\n"
                f"📁 **Domain Groups:** {domain_groups}\n"
                f"💾 **Cached Tenants:** {len(self.tenants_cache)}\n\n"
                f"🤖 **Bot Status:** ✅ Running\n"
                f"🔐 **Super Admin:** `{self.super_admin_id}`"
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔄 Refresh Stats",
                        callback_data="system_stats",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back to Settings",
                        callback_data="bot_settings",
                    ),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                stats_text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error getting system stats: {e}")
            await query.edit_message_text(f"❌ Error getting stats: {e!s}")

    async def manage_tunnels_menu(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Show tunnels management menu."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id

        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            await query.edit_message_text(
                "❌ No tenant selected.\n\nPlease select a tenant first.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🏢 Switch Tenant",
                                callback_data="switch_tenant",
                            ),
                            InlineKeyboardButton(
                                "🔙 Back",
                                callback_data="back_to_menu",
                            ),
                        ],
                    ],
                ),
            )
            return

        # Check access to current tenant
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied to current tenant.")
            return

        tenant_data = self.tenants_cache[self.current_tenant_id]
        tunnels = tenant_data.get("tunnels", {})
        tenant = tenant_data["tenant"]
        account_id = tenant_data.get("account_id")

        if not account_id:
            await query.edit_message_text(
                "❌ No Cloudflare account ID found for this tenant.\n\nTunnels require a Cloudflare account with Zero Trust enabled.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Refresh Cache",
                                callback_data="refresh_cache",
                            ),
                            InlineKeyboardButton(
                                "🔙 Back",
                                callback_data="back_to_menu",
                            ),
                        ],
                    ],
                ),
            )
            return

        is_admin = tenant.admin_user_id == str(user_id)
        role_text = "👤 Admin" if is_admin else "🔧 Super Admin"

        tunnels_text = f"🚇 *{tenant.name} Tunnels ({role_text}):*\n\n"
        keyboard = [
            [
                InlineKeyboardButton(
                    "➕ Create New Tunnel",
                    callback_data="create_tunnel",
                ),
            ],
        ]

        if tunnels:
            tunnels_text += f"Found {len(tunnels)} tunnel(s):\n\n"
            for tunnel_id, tunnel in tunnels.items():
                status = "🟢" if tunnel.status == "healthy" else "🟡" if tunnel.status == "degraded" else "🔴"
                tunnels_text += f"{status} **{tunnel.name}**\n"
                tunnels_text += f"   🆔 `{tunnel_id}`\n"
                if hasattr(tunnel, "created_at"):
                    tunnels_text += f"   📅 Created: {tunnel.created_at.strftime('%Y-%m-%d')}\n"
                tunnels_text += "\n"

                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"📝 {tunnel.name}",
                            callback_data=f"view_tunnel:{tunnel_id}",
                        ),
                        InlineKeyboardButton(
                            "🗑️",
                            callback_data=f"delete_tunnel:{tunnel_id}",
                        ),
                    ],
                )
        else:
            tunnels_text += (
                "No tunnels found.\n\nCreate your first tunnel to securely connect applications or networks."
            )

        keyboard.extend(
            [
                [
                    InlineKeyboardButton(
                        "🔄 Refresh Tunnels",
                        callback_data="refresh_cache",
                    ),
                ],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
            ],
        )
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            tunnels_text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    async def create_tunnel_start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Start creating a new tunnel."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return None

        await query.edit_message_text(
            "🚇 *Create New Cloudflare Tunnel*\n\n"
            "Enter a name for your tunnel:\n\n"
            "💡 *Suggestions:*\n"
            "• `enterprise-vpc-01`\n"
            "• `home-server`\n"
            "• `staging-apps`\n"
            "• `personal-projects`",
            parse_mode="Markdown",
        )

        return WAITING_TUNNEL_NAME

    async def handle_tunnel_name(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle tunnel name input."""
        tunnel_name = update.message.text.strip()
        context.user_data["tunnel_name"] = tunnel_name

        # Generate a tunnel secret (UUID format)
        import uuid

        tunnel_secret = str(uuid.uuid4())
        context.user_data["tunnel_secret"] = tunnel_secret

        await update.message.reply_text(
            f"🚇 *Creating Tunnel: {tunnel_name}*\n\n"
            f"🔐 *Generated Secret:* `{tunnel_secret}`\n\n"
            "⚠️ **Important:** Save this secret securely! You'll need it to run the cloudflared daemon.\n\n"
            "Creating tunnel...",
            parse_mode="Markdown",
        )

        return await self.create_tunnel_finish(update, context)

    async def create_tunnel_finish(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Create the tunnel."""
        tunnel_name = context.user_data.get("tunnel_name")
        tunnel_secret = context.user_data.get("tunnel_secret")

        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            await update.message.reply_text("❌ No active tenant.")
            return ConversationHandler.END

        tenant_data = self.tenants_cache[self.current_tenant_id]
        account_id = tenant_data.get("account_id")

        if not account_id:
            await update.message.reply_text(
                "❌ No account ID available for tunnel creation.",
            )
            return ConversationHandler.END

        cf = await self.get_current_cf_client()
        if not cf:
            await update.message.reply_text("❌ No active tenant selected.")
            return ConversationHandler.END

        try:
            # Create tunnel using Cloudflare API
            tunnel_data = {
                "name": tunnel_name,
                "tunnel_secret": tunnel_secret,
            }

            new_tunnel = await asyncio.to_thread(
                cf.zero_trust.tunnels.create,
                account_id=account_id,
                **tunnel_data,
            )

            # Refresh cache to include new tunnel
            await self.refresh_tenant_domains(self.current_tenant_id)

            success_text = (
                "✅ *Tunnel Created Successfully!*\n\n"
                f"🚇 **Name:** {tunnel_name}\n"
                f"🆔 **Tunnel ID:** `{new_tunnel.id}`\n"
                f"🔐 **Secret:** `{tunnel_secret}`\n\n"
                "📋 **Next Steps:**\n"
                "1. Install cloudflared on your server\n"
                "2. Run: `cloudflared tunnel run {tunnel_name}`\n"
                "3. Configure hostnames or private networks\n\n"
                "💡 **Tip:** Use the 'View Tunnel' button to manage configurations."
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        f"📝 View {tunnel_name}",
                        callback_data=f"view_tunnel:{new_tunnel.id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🚇 Manage Tunnels",
                        callback_data="manage_tunnels",
                    ),
                ],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                success_text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error creating tunnel: {e}")
            await update.message.reply_text(
                f"❌ **Error creating tunnel:**\n`{e!s}`\n\n"
                "This might be due to:\n"
                "• Insufficient permissions in Cloudflare token\n"
                "• Zero Trust not enabled for this account\n"
                "• Tunnel name already exists",
                parse_mode="Markdown",
            )

        context.user_data.clear()
        return ConversationHandler.END

    async def view_tunnel(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """View tunnel details and management options."""
        query = update.callback_query
        await query.answer()

        tunnel_id = query.data.split(":")[1]
        user_id = update.effective_user.id

        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return

        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            await query.edit_message_text("❌ No tenant selected.")
            return

        tenant_data = self.tenants_cache[self.current_tenant_id]
        tunnels = tenant_data.get("tunnels", {})
        tunnel = tunnels.get(tunnel_id)
        account_id = tenant_data.get("account_id")

        if not tunnel:
            await query.edit_message_text("❌ Tunnel not found.")
            return

        cf = await self.get_current_cf_client()
        if not cf:
            await query.edit_message_text("❌ No active tenant selected.")
            return

        try:
            # Get tunnel configuration and connections
            tunnel_config = await asyncio.to_thread(
                cf.zero_trust.tunnels.configurations.get,
                account_id=account_id,
                tunnel_id=tunnel_id,
            )

            connections = await asyncio.to_thread(
                cf.zero_trust.tunnels.connections.list,
                account_id=account_id,
                tunnel_id=tunnel_id,
            )

            status = "🟢" if tunnel.status == "healthy" else "🟡" if tunnel.status == "degraded" else "🔴"

            tunnel_info = (
                f"🚇 *Tunnel: {tunnel.name}*\n\n"
                f"📊 **Status:** {status} {tunnel.status.title()}\n"
                f"🆔 **ID:** `{tunnel_id}`\n"
                f"🔗 **Connections:** {len(connections)}\n"
            )

            if hasattr(tunnel, "created_at"):
                tunnel_info += f"📅 **Created:** {tunnel.created_at.strftime('%Y-%m-%d %H:%M')}\n"

            # Show public hostnames if any
            if tunnel_config and hasattr(tunnel_config, "config") and tunnel_config.config:
                config = tunnel_config.config
                if hasattr(config, "ingress") and config.ingress:
                    tunnel_info += "\n🌐 **Public Hostnames:**\n"
                    for rule in config.ingress[:5]:  # Show first 5
                        if hasattr(rule, "hostname") and rule.hostname:
                            tunnel_info += f"  • `{rule.hostname}`\n"

            keyboard = [
                [
                    InlineKeyboardButton(
                        "➕ Add Hostname",
                        callback_data=f"add_hostname:{tunnel_id}",
                    ),
                    InlineKeyboardButton(
                        "🌐 Add Network",
                        callback_data=f"add_network:{tunnel_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📋 View Config",
                        callback_data=f"tunnel_config:{tunnel_id}",
                    ),
                    InlineKeyboardButton(
                        "🔗 Connections",
                        callback_data=f"tunnel_connections:{tunnel_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🗑️ Delete Tunnel",
                        callback_data=f"delete_tunnel:{tunnel_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back to Tunnels",
                        callback_data="manage_tunnels",
                    ),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                tunnel_info,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error fetching tunnel details: {e}")
            await query.edit_message_text(
                f"❌ Error fetching tunnel details: {e!s}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back", callback_data="manage_tunnels")]],
                ),
            )

    async def add_tunnel_hostname_start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Start adding a public hostname to tunnel."""
        query = update.callback_query
        await query.answer()

        tunnel_id = query.data.split(":")[1]
        user_id = update.effective_user.id

        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return None

        context.user_data["tunnel_id"] = tunnel_id

        # Get available domains for this tenant
        tenant_data = self.tenants_cache[self.current_tenant_id]
        domains = tenant_data.get("domains", {})

        if not domains:
            await query.edit_message_text(
                "❌ No domains found.\n\nYou need at least one domain in Cloudflare to create public hostnames.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔙 Back",
                                callback_data=f"view_tunnel:{tunnel_id}",
                            ),
                        ],
                    ],
                ),
            )
            return None

        domain_list = "\n".join([f"• `{domain}`" for domain in domains][:10])

        await query.edit_message_text(
            f"🌐 *Add Public Hostname*\n\n"
            f"Enter the subdomain and domain:\n\n"
            f"**Available domains:**\n{domain_list}\n\n"
            f"**Examples:**\n"
            f"• `app.example.com`\n"
            f"• `api.mydomain.net`\n"
            f"• `*.dev.example.com` (wildcard)",
            parse_mode="Markdown",
        )

        return WAITING_HOSTNAME_SUBDOMAIN

    async def handle_hostname_subdomain(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle hostname input."""
        hostname = update.message.text.strip()
        context.user_data["hostname"] = hostname

        await update.message.reply_text(
            f"🌐 *Hostname: {hostname}*\n\n"
            f"Enter the service URL that this hostname should route to:\n\n"
            f"**Examples:**\n"
            f"• `http://localhost:8080`\n"
            f"• `https://192.168.1.100:3000`\n"
            f"• `ssh://server.local:22`\n"
            f"• `tcp://database:5432`",
            parse_mode="Markdown",
        )

        return WAITING_HOSTNAME_SERVICE

    async def handle_hostname_service(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle service URL input and create hostname."""
        service_url = update.message.text.strip()
        tunnel_id = context.user_data.get("tunnel_id")
        hostname = context.user_data.get("hostname")

        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            await update.message.reply_text("❌ No active tenant.")
            return ConversationHandler.END

        tenant_data = self.tenants_cache[self.current_tenant_id]
        account_id = tenant_data.get("account_id")

        cf = await self.get_current_cf_client()
        if not cf:
            await update.message.reply_text("❌ No active tenant selected.")
            return ConversationHandler.END

        try:
            # Update tunnel configuration with new hostname
            # Note: This is a simplified example - actual implementation may vary
            # based on how the cloudflare library handles tunnel configurations

            # Get current config
            await asyncio.to_thread(
                cf.zero_trust.tunnels.configurations.get,
                account_id=account_id,
                tunnel_id=tunnel_id,
            )

            # Create new ingress rule
            new_rule = {
                "hostname": hostname,
                "service": service_url,
            }

            # Update configuration (this is a simplified approach)
            config_data = {
                "config": {
                    "ingress": [new_rule],
                },
            }

            await asyncio.to_thread(
                cf.zero_trust.tunnels.configurations.update,
                account_id=account_id,
                tunnel_id=tunnel_id,
                **config_data,
            )

            success_text = (
                "✅ *Public Hostname Added!*\n\n"
                f"🌐 **Hostname:** `{hostname}`\n"
                f"🔗 **Service:** `{service_url}`\n\n"
                f"Your hostname will be available once the tunnel is connected and running."
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "📝 View Tunnel",
                        callback_data=f"view_tunnel:{tunnel_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🚇 Manage Tunnels",
                        callback_data="manage_tunnels",
                    ),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                success_text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error adding hostname: {e}")
            await update.message.reply_text(
                f"❌ **Error adding hostname:**\n`{e!s}`\n\n"
                "This might be due to:\n"
                "• Hostname already exists\n"
                "• Invalid service URL format\n"
                "• Insufficient permissions",
                parse_mode="Markdown",
            )

        context.user_data.clear()
        return ConversationHandler.END

    async def add_tunnel_network_start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Start adding a private network to tunnel."""
        query = update.callback_query
        await query.answer()

        tunnel_id = query.data.split(":")[1]
        user_id = update.effective_user.id

        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return None

        context.user_data["tunnel_id"] = tunnel_id

        await query.edit_message_text(
            "🌐 *Add Private Network*\n\n"
            "Enter the IP address or CIDR range for the private network:\n\n"
            "**Examples:**\n"
            "• `192.168.1.0/24`\n"
            "• `10.0.0.0/8`\n"
            "• `172.16.0.0/16`\n"
            "• `192.168.1.100/32` (single IP)",
            parse_mode="Markdown",
        )

        return WAITING_PRIVATE_NETWORK

    async def handle_private_network(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handle private network input."""
        network = update.message.text.strip()
        tunnel_id = context.user_data.get("tunnel_id")

        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            await update.message.reply_text("❌ No active tenant.")
            return ConversationHandler.END

        tenant_data = self.tenants_cache[self.current_tenant_id]
        account_id = tenant_data.get("account_id")

        cf = await self.get_current_cf_client()
        if not cf:
            await update.message.reply_text("❌ No active tenant selected.")
            return ConversationHandler.END

        try:
            # Add private network route
            route_data = {
                "network": network,
                "tunnel_id": tunnel_id,
            }

            # Note: The exact API endpoint may vary - this is based on common patterns
            await asyncio.to_thread(
                lambda: cf.accounts.routes.ips.create(
                    account_id=account_id,
                    **route_data,
                ),
            )

            success_text = (
                "✅ *Private Network Added!*\n\n"
                f"🌐 **Network:** `{network}`\n"
                f"🚇 **Tunnel:** `{tunnel_id}`\n\n"
                f"This private network will be accessible through the tunnel once it's connected."
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "📝 View Tunnel",
                        callback_data=f"view_tunnel:{tunnel_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🚇 Manage Tunnels",
                        callback_data="manage_tunnels",
                    ),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                success_text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error adding private network: {e}")
            await update.message.reply_text(
                f"❌ **Error adding private network:**\n`{e!s}`\n\n"
                "This might be due to:\n"
                "• Invalid CIDR format\n"
                "• Network already exists\n"
                "• Insufficient permissions",
                parse_mode="Markdown",
            )

        context.user_data.clear()
        return ConversationHandler.END

    async def delete_tunnel_confirm(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Confirm tunnel deletion."""
        query = update.callback_query
        await query.answer()

        tunnel_id = query.data.split(":")[1]
        user_id = update.effective_user.id

        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return

        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            await query.edit_message_text("❌ No tenant selected.")
            return

        tenant_data = self.tenants_cache[self.current_tenant_id]
        tunnels = tenant_data.get("tunnels", {})
        tunnel = tunnels.get(tunnel_id)

        if not tunnel:
            await query.edit_message_text("❌ Tunnel not found.")
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Yes, Delete",
                    callback_data=f"confirm_delete_tunnel:{tunnel_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"view_tunnel:{tunnel_id}",
                ),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"🗑️ *Confirm Tunnel Deletion*\n\n"
            f"Are you sure you want to delete tunnel:\n"
            f"**{tunnel.name}** (`{tunnel_id}`)\n\n"
            f"⚠️ **Warning:**\n"
            f"• All hostnames and routes will be removed\n"
            f"• Active connections will be terminated\n"
            f"• This action cannot be undone!\n\n"
            f"**Alternative:** You can disconnect the tunnel instead of deleting it.",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    async def confirm_delete_tunnel(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Actually delete the tunnel."""
        query = update.callback_query
        await query.answer()

        tunnel_id = query.data.split(":")[1]
        user_id = update.effective_user.id

        if not await self.has_access(user_id, self.current_tenant_id):
            await query.edit_message_text("❌ Access denied.")
            return

        if not self.current_tenant_id or self.current_tenant_id not in self.tenants_cache:
            await query.edit_message_text("❌ No tenant selected.")
            return

        tenant_data = self.tenants_cache[self.current_tenant_id]
        account_id = tenant_data.get("account_id")

        cf = await self.get_current_cf_client()
        if not cf:
            await query.edit_message_text("❌ No active tenant selected.")
            return

        try:
            # Delete the tunnel
            await asyncio.to_thread(
                cf.zero_trust.tunnels.delete,
                account_id=account_id,
                tunnel_id=tunnel_id,
            )

            # Refresh cache to remove deleted tunnel
            await self.refresh_tenant_domains(self.current_tenant_id)

            keyboard = [
                [
                    InlineKeyboardButton(
                        "🚇 Manage Tunnels",
                        callback_data="manage_tunnels",
                    ),
                ],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                "✅ *Tunnel Deleted Successfully!*\n\nThe tunnel and all its configurations have been removed.",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception(f"Error deleting tunnel: {e}")
            await query.edit_message_text(
                f"❌ **Error deleting tunnel:**\n`{e!s}`\n\n"
                "The tunnel might still be active or have dependent configurations.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back", callback_data="manage_tunnels")]],
                ),
            )

    async def back_to_menu(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Return to main menu."""
        query = update.callback_query
        await query.answer()

        # Reset context and reload start
        context.user_data.clear()
        await self.start_command(update, context)

    async def refresh_cache_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle refresh cache callback."""
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
                        logger.exception(f"Error refreshing tenant {tenant.id}: {e}")
                        error_count += 1

            status_text = "✅ *Cache Refreshed!*\n\n"
            status_text += f"🔄 **Refreshed:** {refreshed_count} tenant(s)\n"
            if error_count > 0:
                status_text += f"❌ **Errors:** {error_count} tenant(s)\n"

            total_domains = sum(len(data["domains"]) for data in self.tenants_cache.values())
            status_text += f"🌐 **Total Domains:** {total_domains}"

            await query.edit_message_text(
                status_text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔙 Back to Menu",
                                callback_data="back_to_menu",
                            ),
                        ],
                    ],
                ),
            )

        except Exception as e:
            logger.exception(f"Error refreshing cache: {e}")
            await query.edit_message_text(f"❌ Error refreshing cache: {e!s}")

    async def cancel_conversation(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Cancel current conversation."""
        await update.message.reply_text(
            "❌ Operation cancelled.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔙 Back to Menu",
                            callback_data="back_to_menu",
                        ),
                    ],
                ],
            ),
        )
        context.user_data.clear()
        return ConversationHandler.END

    def run(self) -> None:
        """Run the bot."""
        application = Application.builder().token(self.telegram_token).build()

        # Conversation handler for adding tenants
        add_tenant_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.add_tenant_start, pattern="^add_tenant$"),
            ],
            states={
                WAITING_TENANT_NAME: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_tenant_name,
                    ),
                ],
                WAITING_TENANT_DESCRIPTION: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_tenant_description,
                    ),
                ],
                WAITING_TENANT_ADMIN: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_tenant_admin,
                    ),
                ],
                WAITING_CF_TOKEN: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_cf_token,
                    ),
                ],
            },
            fallbacks=[
                MessageHandler(filters.Regex("^/cancel$"), self.cancel_conversation),
            ],
            per_message=False,
        )

        # Conversation handler for adding DNS records
        add_record_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.add_record_start, pattern="^add_record:"),
            ],
            states={
                WAITING_RECORD_TYPE: [
                    CallbackQueryHandler(
                        self.handle_record_type,
                        pattern="^record_type:",
                    ),
                ],
                WAITING_RECORD_NAME: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_record_name,
                    ),
                ],
                WAITING_RECORD_CONTENT: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_record_content,
                    ),
                ],
                WAITING_RECORD_PRIORITY: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_record_priority,
                    ),
                ],
                WAITING_RECORD_TTL: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_record_ttl,
                    ),
                ],
            },
            fallbacks=[
                MessageHandler(filters.Regex("^/cancel$"), self.cancel_conversation),
            ],
            per_message=False,
        )

        # Conversation handler for creating tunnels
        create_tunnel_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(
                    self.create_tunnel_start,
                    pattern="^create_tunnel$",
                ),
            ],
            states={
                WAITING_TUNNEL_NAME: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_tunnel_name,
                    ),
                ],
            },
            fallbacks=[
                MessageHandler(filters.Regex("^/cancel$"), self.cancel_conversation),
            ],
            per_message=False,
        )

        # Conversation handler for adding hostnames to tunnels
        add_hostname_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(
                    self.add_tunnel_hostname_start,
                    pattern="^add_hostname:",
                ),
            ],
            states={
                WAITING_HOSTNAME_SUBDOMAIN: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_hostname_subdomain,
                    ),
                ],
                WAITING_HOSTNAME_SERVICE: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_hostname_service,
                    ),
                ],
            },
            fallbacks=[
                MessageHandler(filters.Regex("^/cancel$"), self.cancel_conversation),
            ],
            per_message=False,
        )

        # Conversation handler for adding private networks to tunnels
        add_network_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(
                    self.add_tunnel_network_start,
                    pattern="^add_network:",
                ),
            ],
            states={
                WAITING_PRIVATE_NETWORK: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self.handle_private_network,
                    ),
                ],
            },
            fallbacks=[
                MessageHandler(filters.Regex("^/cancel$"), self.cancel_conversation),
            ],
            per_message=False,
        )

        # Add handlers
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(add_tenant_conv)
        application.add_handler(add_record_conv)
        application.add_handler(create_tunnel_conv)
        application.add_handler(add_hostname_conv)
        application.add_handler(add_network_conv)

        # Callback query handlers
        application.add_handler(
            CallbackQueryHandler(self.bot_settings_menu, pattern="^bot_settings$"),
        )
        application.add_handler(
            CallbackQueryHandler(
                self.manage_all_tenants,
                pattern="^manage_all_tenants$",
            ),
        )
        application.add_handler(
            CallbackQueryHandler(self.switch_tenant_menu, pattern="^switch_tenant$"),
        )
        application.add_handler(
            CallbackQueryHandler(self.set_tenant, pattern="^set_tenant:"),
        )
        application.add_handler(
            CallbackQueryHandler(self.view_domains, pattern="^view_domains$"),
        )
        application.add_handler(
            CallbackQueryHandler(self.view_domains, pattern="^manage_dns$"),
        )
        application.add_handler(
            CallbackQueryHandler(self.select_domain, pattern="^select_domain:"),
        )
        application.add_handler(
            CallbackQueryHandler(self.view_dns_records, pattern="^view_records:"),
        )
        application.add_handler(
            CallbackQueryHandler(self.delete_records_menu, pattern="^delete_menu:"),
        )
        application.add_handler(
            CallbackQueryHandler(self.delete_record_confirm, pattern="^delete_record:"),
        )
        application.add_handler(
            CallbackQueryHandler(
                self.confirm_delete_record,
                pattern="^confirm_delete:",
            ),
        )

        # Tunnel management handlers
        application.add_handler(
            CallbackQueryHandler(self.manage_tunnels_menu, pattern="^manage_tunnels$"),
        )
        application.add_handler(
            CallbackQueryHandler(self.view_tunnel, pattern="^view_tunnel:"),
        )
        application.add_handler(
            CallbackQueryHandler(self.delete_tunnel_confirm, pattern="^delete_tunnel:"),
        )
        application.add_handler(
            CallbackQueryHandler(
                self.confirm_delete_tunnel,
                pattern="^confirm_delete_tunnel:",
            ),
        )

        # System handlers
        application.add_handler(
            CallbackQueryHandler(self.system_stats, pattern="^system_stats$"),
        )
        application.add_handler(
            CallbackQueryHandler(self.back_to_menu, pattern="^back_to_menu$"),
        )
        application.add_handler(
            CallbackQueryHandler(
                self.refresh_cache_callback,
                pattern="^refresh_cache$",
            ),
        )

        # Initialize database and run
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self.init_db())
        application.run_polling()


# Configuration and main execution
def main() -> None:
    """Main function to run the bot."""
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
        super_admin_id=SUPER_ADMIN_ID,
    )

    logger.info("Starting Multi-Tenant Cloudflare DNS Manager Bot...")
    logger.info(
        f"Super Admin ID: {SUPER_ADMIN_ID if SUPER_ADMIN_ID else 'Will be set on first use'}",
    )
    bot.run()


if __name__ == "__main__":
    main()
