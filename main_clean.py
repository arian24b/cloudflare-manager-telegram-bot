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
