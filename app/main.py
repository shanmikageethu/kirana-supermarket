"""
app/main.py
-------------
Entry point. Loads secrets from a .env file (never hardcode these, never
commit .env to git — check your .gitignore already covers it) and starts
the Telegram bot, backed by Google Gemini (free tier).

Run from the project root:
    python3 -m app.main
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()  # reads .env in the project root into os.environ


def main():
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")

    missing = []
    if not telegram_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not gemini_api_key:
        missing.append("GEMINI_API_KEY")
    if missing:
        print(f"Missing required environment variable(s): {', '.join(missing)}")
        print("Create a .env file in the project root with:")
        print("  TELEGRAM_BOT_TOKEN=your-token-from-BotFather")
        print("  GEMINI_API_KEY=your-key-from-aistudio.google.com")
        sys.exit(1)

    from telegram_bot.bot_gemini import run  # imported after env is loaded
    run(telegram_token, gemini_api_key)


if __name__ == "__main__":
    main()
