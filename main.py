"""
Pantera - Personal Assistant
Main entry point.
"""
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Load secrets into os.environ so database.py can read DATABASE_PUBLIC_URL etc.
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".config" / "secrets.env")

import uvicorn

from app.config import get_settings
from app.bot.bot import create_bot
from app.db.database import init_db
from app.web.api import app as web_app

# Railway treats stderr as error-level. Route INFO/DEBUG to stdout so they display correctly.
class InfoFilter(logging.Filter):
    def filter(self, rec):
        return rec.levelno in (logging.DEBUG, logging.INFO)

_fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_h_stdout = logging.StreamHandler(sys.stdout)
_h_stdout.setLevel(logging.DEBUG)
_h_stdout.addFilter(InfoFilter())
_h_stdout.setFormatter(logging.Formatter(_fmt))
_h_stderr = logging.StreamHandler(sys.stderr)
_h_stderr.setLevel(logging.WARNING)
_h_stderr.setFormatter(logging.Formatter(_fmt))
_root.addHandler(_h_stdout)
_root.addHandler(_h_stderr)
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

    # Shutdown event for graceful stop (run_webhook/run_polling use run_until_complete
    # internally, which conflicts with asyncio.run - we use manual startup instead)
    stop_event = asyncio.Event()
    web_task = None
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(
                sig, stop_event.set
            )
    except (ValueError, OSError):
        # add_signal_handler not available on Windows or when not in main thread
        pass

    try:
        await application.initialize()
        await application.start()

        # Start web UI on port 3000 (runs alongside bot)
        web_port = int(os.environ.get("WEB_PORT", 3000))
        web_config = uvicorn.Config(web_app, host="0.0.0.0", port=web_port, log_level="warning")
        web_server = uvicorn.Server(web_config)
        web_task = asyncio.create_task(web_server.serve())
        logger.info(f"Web UI: http://localhost:{web_port}")

        if webhook_base:
            # Webhook mode: Telegram pushes updates to our URL. Use this in production
            # (Railway, etc.) to avoid "Conflict: only one bot instance" errors.
            port = int(os.environ.get("PORT", 8080))
            webhook_url = webhook_base.rstrip("/")
            if not webhook_url.endswith("/webhook"):
                webhook_url = webhook_url + "/webhook"
            logger.info("Starting Telegram bot (webhook mode)...")
            await application.updater.start_webhook(
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
            await application.updater.start_polling(drop_pending_updates=True)

        logger.info("Pantera is running. Press Ctrl+C or send SIGTERM to stop.")
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Stopping Pantera...")
        if web_task is not None:
            web_task.cancel()
            try:
                await web_task
            except asyncio.CancelledError:
                pass
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Pantera stopped.")


if __name__ == "__main__":
    asyncio.run(main())
