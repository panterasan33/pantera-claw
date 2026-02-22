"""
Pantera - Personal Assistant
Main entry point.
"""
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

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
    
    # Create and run bot
    application = create_bot()
    
    logger.info("Starting Telegram bot...")
    
    # Initialize and start polling
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    
    logger.info("🐆 Pantera is live!")
    
    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down...")
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Pantera stopped.")


if __name__ == "__main__":
    asyncio.run(main())
