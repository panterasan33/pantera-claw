"""
Task creation and date parsing for bot-captured tasks.
"""
import re
from datetime import datetime, timedelta
from typing import Optional

from app.db.database import AsyncSessionLocal
from app.models.task import Task, TaskStatus


def parse_due_date(due_str: str) -> Optional[datetime]:
    """
    Parse natural language due date (e.g. 'today', 'tomorrow') to datetime.
    Returns None if unparseable.
    """
    if not due_str or not isinstance(due_str, str):
        return None
    s = due_str.strip().lower()
    now = datetime.now()
    today = now.replace(hour=23, minute=59, second=59, microsecond=0)  # End of day

    # Common phrases
    if s in ("today", "tonight"):
        return today
    if s in ("tomorrow", "tmr"):
        return (now + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
    if s in ("next week", "next week."):
        return (now + timedelta(days=7)).replace(hour=23, minute=59, second=59, microsecond=0)
    if s in ("next month",):
        # Approximate: add 30 days
        return (now + timedelta(days=30)).replace(hour=23, minute=59, second=59, microsecond=0)

    # "in N days/weeks/months"
    in_match = re.match(r"in\s+(\d+)\s+(day|days|week|weeks|month|months)", s)
    if in_match:
        n = int(in_match.group(1))
        unit = in_match.group(2)
        if unit.startswith("day"):
            return (now + timedelta(days=n)).replace(hour=23, minute=59, second=59, microsecond=0)
        elif unit.startswith("week"):
            return (now + timedelta(weeks=n)).replace(hour=23, minute=59, second=59, microsecond=0)
        elif unit.startswith("month"):
            return (now + timedelta(days=30 * n)).replace(hour=23, minute=59, second=59, microsecond=0)

    # "next/this monday/tuesday/..."
    weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
                "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    day_match = re.match(r"(next|this)\s+(\w+)", s)
    if day_match:
        which = day_match.group(1)
        day_name = day_match.group(2)
        if day_name in weekdays:
            target = weekdays[day_name]
            current = now.weekday()
            days_ahead = (target - current) % 7
            if which == "next" and days_ahead == 0:
                days_ahead = 7
            elif which == "this" and days_ahead == 0:
                days_ahead = 0
            return (now + timedelta(days=days_ahead)).replace(hour=23, minute=59, second=59, microsecond=0)
    # bare weekday name (e.g. "friday")
    if s in weekdays:
        target = weekdays[s]
        current = now.weekday()
        days_ahead = (target - current) % 7
        if days_ahead == 0:
            days_ahead = 7  # assume next occurrence
        return (now + timedelta(days=days_ahead)).replace(hour=23, minute=59, second=59, microsecond=0)

    # Try ISO format (YYYY-MM-DD)
    iso_match = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if iso_match:
        try:
            y, m, d = int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))
            return datetime(y, m, d, 23, 59, 59)
        except ValueError:
            pass

    # Try MM/DD or MM-DD
    slash_match = re.match(r"(\d{1,2})[/\-](\d{1,2})", s)
    if slash_match:
        try:
            m, d = int(slash_match.group(1)), int(slash_match.group(2))
            y = now.year
            return datetime(y, m, d, 23, 59, 59)
        except ValueError:
            pass

    return None


async def create_task_from_classification(
    title: str,
    notes: Optional[str] = None,
    due_date_str: Optional[str] = None,
    project: Optional[str] = None,
    group: Optional[str] = None,
    telegram_message_id: Optional[int] = None,
) -> Optional[int]:
    """
    Create a task in the database from classifier-extracted data.
    Returns the created task id or None on failure.
    """
    due_date = parse_due_date(due_date_str) if due_date_str else None

    async with AsyncSessionLocal() as session:
        try:
            task = Task(
                title=title,
                notes=notes,
                due_date=due_date,
                project=project,
                group=group,
                status=TaskStatus.NOT_STARTED,
                source_type="text",
                telegram_message_id=telegram_message_id,
            )
            session.add(task)
            await session.flush()
            task_id = task.id
            await session.commit()
            return task_id
        except Exception:
            await session.rollback()
            raise
