"""
Pantera - Personal Assistant
Main entry point.
"""
import asyncio
import json
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


class JsonFormatter(logging.Formatter):
    """Format logs as JSON for Railway's Log Explorer (parses level, message, time)."""

    def format(self, record):
        log_obj = {
            "time": self.formatTime(record, self.datefmt or "%Y-%m-%d %H:%M:%S"),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        if record.name != "root":
            log_obj["logger"] = record.name
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


# Railway requires JSON logs with "level" and "message" to show correct severity.
# Plain text logs are treated as stderr→error, stdout→info and often mislabeled.
_use_json = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("JSON_LOGS"))
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.handlers.clear()  # Avoid duplicate handlers from libraries
_handler = logging.StreamHandler(sys.stdout)
_handler.setLevel(logging.DEBUG)
_handler.setFormatter(
    JsonFormatter() if _use_json
    else logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
_root.addHandler(_handler)
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

    # Single port for both web UI and webhook (Railway exposes only one port)
    port = int(os.environ.get("PORT", 3000))
    web_app.state.bot_application = application

    try:
        await application.initialize()
        await application.start()

        if webhook_base:
            # Webhook mode: register with Telegram, serve via FastAPI /webhook route
            webhook_url = webhook_base.rstrip("/")
            if not webhook_url.endswith("/webhook"):
                webhook_url = webhook_url + "/webhook"
            await application.bot.set_webhook(
                url=webhook_url,
                drop_pending_updates=True,
            )
            logger.info(f"Webhook registered: {webhook_url}")
        else:
            # Polling mode: we poll Telegram for updates. Use for local dev only.
            logger.info("Starting Telegram bot (polling mode)...")
            await application.updater.start_polling(drop_pending_updates=True)

        logger.info(f"Web UI + API: http://0.0.0.0:{port}")

        web_config = uvicorn.Config(
            web_app, host="0.0.0.0", port=port, log_level="warning"
        )
        web_server = uvicorn.Server(web_config)
        web_task = asyncio.create_task(web_server.serve())

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
        if not webhook_base:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Pantera stopped.")


if __name__ == "__main__":
    asyncio.run(main())
