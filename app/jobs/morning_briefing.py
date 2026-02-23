"""
Morning briefing job - sends daily summary via Telegram.
"""
import logging
from datetime import date, datetime

from app.db.database import AsyncSessionLocal
from app.models.task import Task, TaskStatus
from app.models.reminder import Reminder
from app.models.memory import MemoryItem, MemoryType
from app.config import get_settings
from sqlalchemy import select

logger = logging.getLogger(__name__)


async def run_morning_briefing(bot_application):
    """Send morning briefing: My Day tasks, upcoming reminders, memory items due."""
    settings = get_settings()
    chat_id = settings.telegram_chat_id
    if not chat_id:
        return
    try:
        chat_id = int(chat_id)
    except (ValueError, TypeError):
        logger.warning("TELEGRAM_CHAT_ID must be a valid integer for morning briefing")
        return

    lines = ["☀️ *Good morning!* Here's your briefing.\n"]

    async with AsyncSessionLocal() as session:
        # My Day tasks
        result = await session.execute(
            select(Task).where(Task.my_day == True).where(Task.status != TaskStatus.DONE).order_by(Task.created_at)
        )
        tasks = result.scalars().all()
        if tasks:
            lines.append("*Planned for today:*")
            for t in tasks[:10]:
                lines.append(f"  • {t.title}")
            if len(tasks) > 10:
                lines.append(f"  ... and {len(tasks) - 10} more")
            lines.append("")

        # Upcoming reminders (next 24h)
        from datetime import timedelta
        now = datetime.now()
        soon = now + timedelta(hours=24)
        result = await session.execute(
            select(Reminder)
            .where(Reminder.is_active == True)
            .where(Reminder.next_trigger >= now)
            .where(Reminder.next_trigger <= soon)
            .order_by(Reminder.next_trigger.asc())
            .limit(5)
        )
        reminders = result.scalars().all()
        if reminders:
            lines.append("*Reminders today:*")
            for r in reminders:
                when = r.next_trigger.strftime("%H:%M") if r.next_trigger else "—"
                lines.append(f"  🔔 {r.content} ({when})")
            lines.append("")

        # Memory items (birthdays, annual events) - check if any fall in next 7 days
        today = date.today()
        result = await session.execute(
            select(MemoryItem).where(MemoryItem.event_date.isnot(None)).order_by(MemoryItem.event_date)
        )
        memories = result.scalars().all()
        upcoming = []
        for m in memories:
            if not m.event_date:
                continue
            ev = m.event_date
            if isinstance(ev, datetime):
                ev = ev.date()
            delta = (ev.replace(year=today.year) - today).days
            if delta < 0:
                delta = (ev.replace(year=today.year + 1) - today).days
            if 0 <= delta <= 7:
                upcoming.append((m, delta))
        if upcoming:
            lines.append("*Coming up:*")
            for m, days in upcoming[:5]:
                when = "today" if days == 0 else f"in {days} days"
                lines.append(f"  🎂 {m.content} ({when})")
            lines.append("")

    if len(lines) <= 1:
        lines.append("Nothing planned. Have a great day!")

    text = "\n".join(lines).strip()
    try:
        await bot_application.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception(f"Morning briefing failed: {e}")
