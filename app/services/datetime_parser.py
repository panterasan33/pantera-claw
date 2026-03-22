"""Shared date/time parsing helpers for tasks, reminders, and memories."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Optional


_MONTHS = {
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

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_MONTH_NAME_DATE_RE = re.compile(
    r"\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"[\s,]+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:[\s,]+(?P<year>\d{4}))?\b",
    flags=re.IGNORECASE,
)

_DATE_NUMERIC_RE = re.compile(
    r"\b(?P<month>\d{1,2})[/-](?P<day>\d{1,2})(?:[/-](?P<year>\d{2,4}))?\b"
)

_ISO_DATE_RE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_IN_DELTA_RE = re.compile(r"\bin\s+(?P<count>\d+)\s+(?P<unit>minute|hour|day|week|month|year)s?\b")
_NEXT_DELTA_RE = re.compile(r"\bnext\s+(?P<unit>week|month|year)\b")
_WEEKDAY_RE = re.compile(
    r"\b(?P<prefix>next\s+)?(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)
_TIME_RE = re.compile(r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?\b")


def _normalize_year(year_text: Optional[str], *, reference: datetime) -> int:
    if not year_text:
        return reference.year
    year = int(year_text)
    if year < 100:
        year += 2000
    return year


def parse_natural_date(text: Optional[str], *, reference: Optional[datetime] = None) -> Optional[date]:
    """Parse a date from natural language without introducing external deps."""
    if not text or not isinstance(text, str):
        return None

    now = reference or datetime.now()
    s = text.strip().lower()

    if s in {"today", "now"}:
        return now.date()
    if s == "tomorrow":
        return (now + timedelta(days=1)).date()

    iso_match = _ISO_DATE_RE.search(s)
    if iso_match:
        try:
            return date(
                int(iso_match.group("year")),
                int(iso_match.group("month")),
                int(iso_match.group("day")),
            )
        except ValueError:
            return None

    numeric_match = _DATE_NUMERIC_RE.search(s)
    if numeric_match:
        try:
            return date(
                _normalize_year(numeric_match.group("year"), reference=now),
                int(numeric_match.group("month")),
                int(numeric_match.group("day")),
            )
        except ValueError:
            return None

    month_match = _MONTH_NAME_DATE_RE.search(s)
    if month_match:
        month = _MONTHS.get(month_match.group("month")[:3].lower())
        if month:
            try:
                return date(
                    _normalize_year(month_match.group("year"), reference=now),
                    month,
                    int(month_match.group("day")),
                )
            except ValueError:
                return None

    delta_match = _IN_DELTA_RE.search(s)
    if delta_match:
        count = int(delta_match.group("count"))
        unit = delta_match.group("unit")
        if unit == "minute":
            return (now + timedelta(minutes=count)).date()
        if unit == "hour":
            return (now + timedelta(hours=count)).date()
        if unit == "day":
            return (now + timedelta(days=count)).date()
        if unit == "week":
            return (now + timedelta(weeks=count)).date()
        if unit == "month":
            return (now + timedelta(days=30 * count)).date()
        if unit == "year":
            return (now + timedelta(days=365 * count)).date()

    next_delta = _NEXT_DELTA_RE.search(s)
    if next_delta:
        unit = next_delta.group("unit")
        if unit == "week":
            return (now + timedelta(days=7)).date()
        if unit == "month":
            return (now + timedelta(days=30)).date()
        if unit == "year":
            return (now + timedelta(days=365)).date()

    weekday_match = _WEEKDAY_RE.search(s)
    if weekday_match:
        target = _WEEKDAYS[weekday_match.group("weekday")]
        days_ahead = (target - now.weekday()) % 7
        if weekday_match.group("prefix") or days_ahead == 0:
            days_ahead = 7 if days_ahead == 0 else days_ahead
        return (now + timedelta(days=days_ahead)).date()

    return None


def parse_natural_time(text: Optional[str], *, default_time: time = time(9, 0)) -> time:
    if not text or not isinstance(text, str):
        return default_time

    s = text.strip().lower()
    if "noon" in s:
        return time(12, 0)
    if "midnight" in s:
        return time(0, 0)
    if "tonight" in s:
        return time(20, 0)

    match = _TIME_RE.search(s)
    if not match:
        return default_time

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = match.group("ampm")
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return default_time
    return time(hour, minute)


def parse_natural_datetime(
    text: Optional[str],
    *,
    reference: Optional[datetime] = None,
    default_time: time = time(9, 0),
    prefer_end_of_day: bool = False,
) -> Optional[datetime]:
    """Parse a natural-language datetime using conservative heuristics."""
    if not text or not isinstance(text, str):
        return None

    now = reference or datetime.now()
    s = text.strip().lower()

    delta_match = _IN_DELTA_RE.search(s)
    if delta_match:
        count = int(delta_match.group("count"))
        unit = delta_match.group("unit")
        if unit == "minute":
            return now + timedelta(minutes=count)
        if unit == "hour":
            return now + timedelta(hours=count)
        if unit == "day":
            return now + timedelta(days=count)
        if unit == "week":
            return now + timedelta(weeks=count)
        if unit == "month":
            return now + timedelta(days=30 * count)
        if unit == "year":
            return now + timedelta(days=365 * count)

    parsed_date = parse_natural_date(s, reference=now)
    if not parsed_date:
        return None

    parsed_time = parse_natural_time(s, default_time=default_time)
    if prefer_end_of_day and _TIME_RE.search(s) is None and "noon" not in s and "midnight" not in s:
        parsed_time = time(23, 59, 59)
    return datetime.combine(parsed_date, parsed_time)
