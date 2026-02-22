"""
Main Telegram bot initialization and setup.
"""
import asyncio
import logging
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from app.config import get_settings
from app.bot.handlers import (
    start_command,
    help_command,
    handle_message,
    handle_callback,
    handle_voice,
    handle_photo,
    handle_document,
)

logger = logging.getLogger(__name__)


def create_bot() -> Application:
    """Create and configure the Telegram bot application."""
    settings = get_settings()
    
    # Create application
    application = Application.builder().token(settings.telegram_bot_token).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    
    # TODO: Add more command handlers
    # application.add_handler(CommandHandler("tasks", tasks_command))
    # application.add_handler(CommandHandler("today", today_command))
    # application.add_handler(CommandHandler("reminders", reminders_command))
    # application.add_handler(CommandHandler("search", search_command))
    
    # Callback query handler (for inline keyboards)
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Message handlers
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("Bot handlers configured")
    return application


async def run_bot():
    """Run the bot in polling mode."""
    application = create_bot()
    
    logger.info("Starting Pantera bot...")
    
    # Initialize and start
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    
    logger.info("Pantera is running! Press Ctrl+C to stop.")
    
    # Run until stopped
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    asyncio.run(run_bot())
