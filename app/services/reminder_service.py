"""
Reminder creation and date parsing.
"""
from datetime import datetime
from typing import Optional, Any

from app.db.database import AsyncSessionLocal
from app.models.reminder import Reminder, ReminderType, RecurrencePattern
from app.services.datetime_parser import parse_natural_datetime


def parse_trigger_time(trigger_str: str) -> Optional[datetime]:
    """
    Parse natural language trigger time (e.g. 'tomorrow 9am', 'in 1 hour').
    Returns None if unparseable.
    """
    return parse_natural_datetime(trigger_str)


def _compute_next_trigger(
    reminder_type: ReminderType,
    trigger_at: Optional[datetime],
    recurrence_pattern: Optional[RecurrencePattern],
    recurrence_config: Optional[dict],
) -> Optional[datetime]:
    """Compute next_trigger for a new reminder."""
    if reminder_type == ReminderType.ONE_OFF:
        return trigger_at
    if reminder_type == ReminderType.RECURRING and trigger_at:
        # For newly created recurring reminders, the first trigger should be the
        # parsed trigger time itself. After each nudge, the scheduler advances
        # next_trigger based on recurrence_pattern.
        return trigger_at
    return trigger_at


async def create_reminder(
    content: str,
    trigger_at_str: Optional[str] = None,
    reminder_type: str = "one_off",
    recurrence_pattern: Optional[str] = None,
    recurrence_config: Optional[dict] = None,
    snooze_minutes: int = 120,
    telegram_message_id: Optional[int] = None,
) -> Optional[int]:
    """Create a reminder. Returns id or None."""
    trigger_at = parse_trigger_time(trigger_at_str) if trigger_at_str else None
    rt = ReminderType.ONE_OFF if reminder_type == "one_off" else ReminderType.RECURRING
    rp = None
    if recurrence_pattern:
        if recurrence_pattern == "quarterly":
            rp = RecurrencePattern.CUSTOM
            recurrence_config = {**(recurrence_config or {}), "interval_days": 90}
        else:
            try:
                rp = RecurrencePattern(recurrence_pattern)
            except ValueError:
                pass

    next_trigger = _compute_next_trigger(rt, trigger_at, rp, recurrence_config)

    async with AsyncSessionLocal() as session:
        try:
            r = Reminder(
                content=content,
                reminder_type=rt,
                trigger_at=trigger_at,
                recurrence_pattern=rp,
                recurrence_config=recurrence_config,
                snooze_minutes=snooze_minutes,
                is_active=True,
                next_trigger=next_trigger or trigger_at,
                source_type="text",
                telegram_message_id=telegram_message_id,
            )
            session.add(r)
            await session.flush()
            try:
                from app.services.embedding_service import embed_text
                emb = await embed_text(content)
                if emb:
                    r.embedding = emb
            except Exception:
                pass
            rid = r.id
            await session.commit()
            return rid
        except Exception:
            await session.rollback()
            raise


async def create_reminder_from_classification(
    content: str,
    trigger_time: Optional[str] = None,
    is_recurring: bool = False,
    recurrence_pattern: Optional[str] = None,
    recurrence_config: Optional[dict] = None,
    telegram_message_id: Optional[int] = None,
) -> Optional[int]:
    """Create reminder from classifier-extracted data."""
    rt = "recurring" if is_recurring else "one_off"
    return await create_reminder(
        content=content,
        trigger_at_str=trigger_time,
        reminder_type=rt,
        recurrence_pattern=recurrence_pattern,
        recurrence_config=recurrence_config,
        telegram_message_id=telegram_message_id,
    )
