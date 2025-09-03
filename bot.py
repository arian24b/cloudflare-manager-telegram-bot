import asyncio
import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from telegram.ext import Application, CommandHandler

from handlers import CloudflareManager
from config import logger

# Load environment variables from .env file
load_dotenv()


class CloudflareBotRunner:
    """Main bot runner class that initializes database and starts the bot."""

    def __init__(self):
        # Environment variables
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///dns_bot.db")
        self.super_admin_id = os.getenv("SUPER_ADMIN_ID")  # Optional
        self.proxy_url = os.getenv("PROXY_URL", "http://127.0.0.1:2080")  # Proxy configuration

        if not self.telegram_token:
            logger.error("TELEGRAM_BOT_TOKEN environment variable is required")
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        # Initialize bot
        self.bot = CloudflareManager(
            telegram_token=self.telegram_token,
            db_url=self.db_url,
            super_admin_id=self.super_admin_id,
        )

        # Database setup
        self.engine = create_async_engine(self.db_url)
        self.SessionLocal = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Set database references in bot
        self.bot.engine = self.engine
        self.bot.SessionLocal = self.SessionLocal

    async def initialize_database(self):
        """Initialize database tables."""
        from models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info("Database initialized successfully")

    def run(self):
        """Run the bot."""
        # Create application
        if not self.telegram_token:
            raise ValueError("Telegram token is required")

        # Create application with proxy support
        import os
        os.environ['HTTPS_PROXY'] = self.proxy_url
        os.environ['HTTP_PROXY'] = self.proxy_url

        application = Application.builder().token(self.telegram_token).build()

        # Initialize database synchronously for now
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.initialize_database())

        # TODO: Add handlers here
        # This will be populated with all the handlers from the original main.py

        # Add command handlers
        application.add_handler(CommandHandler("start", self.bot.start_command))
        application.add_handler(CommandHandler("help", self.bot.help_command))
        application.add_handler(CommandHandler("status", self.bot.status_command))
        application.add_handler(CommandHandler("tenants", self.bot.tenants_command))
        application.add_handler(CommandHandler("my_tenants", self.bot.my_tenants_command))
        application.add_handler(CommandHandler("domains", self.bot.domains_command))
        application.add_handler(CommandHandler("refresh", self.bot.refresh_command))
        application.add_handler(CommandHandler("add_tenant", self.bot.add_tenant_command))
        application.add_handler(CommandHandler("connect_cf", self.bot.connect_cf_command))
        application.add_handler(CommandHandler("tenant_info", self.bot.tenant_info_command))

        logger.info("Starting Multi-Tenant Cloudflare DNS Manager Bot...")
        logger.info(
            f"Super Admin ID: {self.super_admin_id if self.super_admin_id else 'Will be set on first use'}",
        )

        # Run the bot
        application.run_polling()
