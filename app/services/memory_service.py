"""Memory capture and event-date parsing for long-horizon memories."""
from __future__ import annotations

from datetime import date
from typing import Optional

from app.models.memory import MemoryType
from app.services.datetime_parser import parse_natural_date


def parse_event_date(event_date_str: Optional[str]) -> Optional[date]:
    """Parse event date from natural-ish input (today, ISO, MM/DD, Month Day)."""
    return parse_natural_date(event_date_str)


def _coerce_memory_type(memory_subtype: Optional[str], is_annual: bool) -> MemoryType:
    subtype = (memory_subtype or "").lower()
    if subtype == "birthday":
        return MemoryType.BIRTHDAY
    if subtype == "annual_event" or is_annual:
        return MemoryType.ANNUAL_EVENT
    return MemoryType.NOTE


async def create_memory_from_classification(
    content: str,
    event_date: Optional[str] = None,
    is_annual: bool = False,
    memory_subtype: Optional[str] = None,
    summary: Optional[str] = None,
    tags: Optional[list[str]] = None,
    original_message: Optional[str] = None,
    source_type: str = "telegram",
    telegram_message_id: Optional[int] = None,
) -> Optional[int]:
    """Persist a memory item extracted from classifier output."""
    from app.db.database import AsyncSessionLocal
    from app.models.memory import MemoryItem

    parsed_date = parse_event_date(event_date)
    mt = _coerce_memory_type(memory_subtype, is_annual)

    recurrence_date = None
    lead_times = None
    if parsed_date and (is_annual or mt in {MemoryType.BIRTHDAY, MemoryType.ANNUAL_EVENT}):
        recurrence_date = {"month": parsed_date.month, "day": parsed_date.day}
        lead_times = [7, 1, 0] if mt == MemoryType.BIRTHDAY else [28, 7, 0]

    async with AsyncSessionLocal() as session:
        item = MemoryItem(
            content=content,
            summary=summary,
            memory_type=mt,
            event_date=parsed_date,
            recurrence_date=recurrence_date,
            lead_times=lead_times,
            tags=tags,
            original_message=original_message,
            source_type=source_type,
            telegram_message_id=telegram_message_id,
        )
        session.add(item)
        await session.flush()

        try:
            from app.services.embedding_service import embed_text

            emb = await embed_text(content)
            if emb:
                item.embedding = emb
        except Exception:
            pass

        memory_id = item.id
        await session.commit()
        return memory_id
