"""
Reminder creation and date parsing.
"""
import re
from datetime import datetime, timedelta
from typing import Optional, Any

from app.db.database import AsyncSessionLocal
from app.models.reminder import Reminder, ReminderType, RecurrencePattern


def parse_trigger_time(trigger_str: str) -> Optional[datetime]:
    """
    Parse natural language trigger time (e.g. 'tomorrow 9am', 'in 1 hour').
    Returns None if unparseable.
    """
    if not trigger_str or not isinstance(trigger_str, str):
        return None
    s = trigger_str.strip().lower()
    now = datetime.now()

    # "in X minutes" / "in X hours"
    in_match = re.match(r"in\s+(\d+)\s+(minute|hour)s?", s)
    if in_match:
        try:
            n = int(in_match.group(1))
            unit = in_match.group(2)
            if unit == "minute":
                return now + timedelta(minutes=n)
            return now + timedelta(hours=n)
        except ValueError:
            pass

    # Extract time (HH:MM, H:MM, 9am, 2pm, etc.)
    hour, minute = 9, 0
    time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?|(\d{1,2})(?::(\d{2}))?", s)
    if time_match:
        if time_match.group(3):  # am/pm
            h = int(time_match.group(1))
            m = int(time_match.group(2) or 0)
            if time_match.group(3) == "pm" and h < 12:
                h += 12
            elif time_match.group(3) == "am" and h == 12:
                h = 0
            hour, minute = h, m
        elif time_match.group(4):
            hour = int(time_match.group(4))
            minute = int(time_match.group(5) or 0)

    if "next year" in s or "yearly" in s or "every year" in s:
        return (now + timedelta(days=365)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if "quarter" in s or "quarterly" in s:
        return (now + timedelta(days=90)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Date part
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if "tomorrow" in s or "tmr" in s:
        base = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    elif "today" in s or "tonight" in s:
        pass
    elif "next week" in s:
        base = (now + timedelta(days=7)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        # Try ISO or MM/DD
        iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        if iso_match:
            try:
                y, m, d = int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))
                base = datetime(y, m, d, hour, minute, 0)
            except ValueError:
                pass
        else:
            slash_match = re.search(r"(\d{1,2})[/\-](\d{1,2})", s)
            if slash_match:
                try:
                    m, d = int(slash_match.group(1)), int(slash_match.group(2))
                    y = now.year
                    base = datetime(y, m, d, hour, minute, 0)
                except ValueError:
                    pass

    return base


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
