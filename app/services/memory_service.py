"""Memory capture and event-date parsing for long-horizon memories."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional
from app.models.memory import MemoryType


_MONTH_NAME_DATE_RE = re.compile(
    r"\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b",
    flags=re.IGNORECASE,
)


_MONTH_MAP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def parse_event_date(event_date_str: Optional[str]) -> Optional[date]:
    """Parse event date from natural-ish input (today, ISO, MM/DD, Month Day)."""
    if not event_date_str or not isinstance(event_date_str, str):
        return None

    s = event_date_str.strip().lower()
    today = date.today()

    if s in {"today", "now"}:
        return today
    if s == "tomorrow":
        return today.fromordinal(today.toordinal() + 1)

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d", "%m-%d"):
        try:
            parsed = datetime.strptime(s, fmt).date()
            if fmt in {"%m/%d", "%m-%d"}:
                return parsed.replace(year=today.year)
            return parsed
        except ValueError:
            continue

    month_name_match = _MONTH_NAME_DATE_RE.search(s)
    if month_name_match:
        month_name = month_name_match.group("month")[:3].lower()
        day = int(month_name_match.group("day"))
        month = _MONTH_MAP.get(month_name)
        if month:
            try:
                return date(today.year, month, day)
            except ValueError:
                return None

    return None


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
            memory_type=mt,
            event_date=parsed_date,
            recurrence_date=recurrence_date,
            lead_times=lead_times,
            source_type="telegram",
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
