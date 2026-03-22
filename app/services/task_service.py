"""
Task creation and date parsing for bot-captured tasks.
"""
from datetime import datetime
from typing import Optional

from app.db.database import AsyncSessionLocal
from app.models.task import Task, TaskStatus
from app.services.datetime_parser import parse_natural_datetime


def parse_due_date(due_str: str) -> Optional[datetime]:
    """
    Parse natural language due date (e.g. 'today', 'tomorrow') to datetime.
    Returns None if unparseable.
    """
    return parse_natural_datetime(due_str, prefer_end_of_day=True)


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
