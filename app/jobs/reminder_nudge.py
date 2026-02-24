"""
Reminder nudge job - sends Telegram messages for due reminders.
"""
import logging
from datetime import datetime, timedelta

from app.db.database import AsyncSessionLocal
from app.models.reminder import Reminder, ReminderType, RecurrencePattern
from app.config import get_settings
from app.bot.handlers import build_reminder_keyboard

logger = logging.getLogger(__name__)


async def run_reminder_nudge(bot_application):
    """Check due reminders and send Telegram nudges."""
    settings = get_settings()
    chat_id = settings.telegram_chat_id
    if not chat_id:
        return
    try:
        chat_id = int(chat_id)
    except (ValueError, TypeError):
        logger.warning("TELEGRAM_CHAT_ID must be a valid integer")
        return

    now = datetime.now()
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(Reminder)
            .where(Reminder.is_active == True)
            .where(Reminder.next_trigger <= now)
            .order_by(Reminder.next_trigger.asc())
        )
        reminders = result.scalars().all()

    for r in reminders:
        try:
            bot = bot_application.bot
            text = f"🔔 *Reminder:* {r.content}"
            if r.next_trigger:
                text += f"\n⏰ Due: {r.next_trigger.strftime('%H:%M')}"
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=build_reminder_keyboard(r.id),
            )

            async with AsyncSessionLocal() as session:
                result = await session.execute(select(Reminder).where(Reminder.id == r.id))
                rem = result.scalar_one_or_none()
                if rem:
                    rem.last_triggered = now
                    if rem.reminder_type == ReminderType.ONE_OFF:
                        rem.is_active = False
                    else:
                        rem.current_cycle_done = False
                        if rem.recurrence_pattern == RecurrencePattern.DAILY:
                            rem.next_trigger = now + timedelta(days=1)
                        elif rem.recurrence_pattern == RecurrencePattern.WEEKLY:
                            rem.next_trigger = now + timedelta(days=7)
                        elif rem.recurrence_pattern == RecurrencePattern.MONTHLY:
                            rem.next_trigger = now + timedelta(days=30)
                        elif rem.recurrence_pattern == RecurrencePattern.YEARLY:
                            rem.next_trigger = now + timedelta(days=365)
                        elif rem.recurrence_pattern == RecurrencePattern.CUSTOM:
                            interval_days = (rem.recurrence_config or {}).get("interval_days", 1)
                            rem.next_trigger = now + timedelta(days=max(int(interval_days), 1))
                        else:
                            rem.next_trigger = now + timedelta(days=1)
                    await session.commit()
        except Exception as e:
            logger.exception(f"Failed to send reminder {r.id}: {e}")
