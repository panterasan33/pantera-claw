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
