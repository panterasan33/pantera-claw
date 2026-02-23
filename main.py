"""
Pantera - Personal Assistant
Main entry point.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Load secrets into os.environ so database.py can read DATABASE_PUBLIC_URL etc.
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".config" / "secrets.env")

from app.config import get_settings
from app.bot.bot import create_bot
from app.db.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    """Main entry point."""
    settings = get_settings()

    logger.info("🐆 Starting Pantera...")

    # Initialize database
    logger.info("Initializing database...")
    try:
        await init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.warning(f"Database init failed (may need Postgres): {e}")

    # Create bot
    application = create_bot()

    # Use webhook mode when WEBHOOK_URL is set, or when RAILWAY_PUBLIC_DOMAIN exists
    # (Railway sets this when public networking is enabled)
    webhook_base = settings.webhook_url or (
        f"https://{os.environ.get('RAILWAY_PUBLIC_DOMAIN', '')}"
        if os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        else ""
    )

    if webhook_base:
        # Webhook mode: Telegram pushes updates to our URL. Use this in production
        # (Railway, etc.) to avoid "Conflict: only one bot instance" errors.
        port = int(os.environ.get("PORT", 8080))
        webhook_url = webhook_base.rstrip("/")
        if not webhook_url.endswith("/webhook"):
            webhook_url = webhook_url + "/webhook"
        logger.info("Starting Telegram bot (webhook mode)...")
        await application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="webhook",
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        # Polling mode: we poll Telegram for updates. Use for local dev only.
        # Only ONE instance can poll per bot token - multiple replicas will conflict.
        logger.info("Starting Telegram bot (polling mode)...")
        await application.run_polling(drop_pending_updates=True)

    logger.info("Pantera stopped.")


if __name__ == "__main__":
    asyncio.run(main())
