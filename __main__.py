#!/usr/bin/env python3
"""
CloudflareManager
A multi-tenant Telegram bot for managing Cloudflare DNS records and tunnels.
"""

from bot import CloudflareBotRunner


def main() -> None:
    """Main entry point for the bot."""
    try:
        bot_runner = CloudflareBotRunner()
        bot_runner.run()
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        print(f"Error starting bot: {e}")
        raise


if __name__ == "__main__":
    main()
