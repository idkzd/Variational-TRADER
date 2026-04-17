#!/usr/bin/env python3
"""
Entry point for the Variational Omni Trading Bot.

Usage:
    python main.py
"""

from bot import TradingBot
from config import BotConfig


def main() -> None:
    """Load configuration and start the trading bot."""
    config = BotConfig()

    issues = config.validate()
    if issues:
        print("❌ Configuration errors:")
        for issue in issues:
            print(f"   • {issue}")
        print("\nPlease fix the issues in your .env file and try again.")
        print("See .env.example for reference.")
        return

    bot = TradingBot(config)
    bot.run()


if __name__ == "__main__":
    main()
