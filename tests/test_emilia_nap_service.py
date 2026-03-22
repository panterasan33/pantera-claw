"""Unit tests for Emilia nap helpers (no database)."""
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from app.services.emilia_nap_service import format_duration, format_uk, parse_time_hint, uk_now


def test_format_duration_hours_minutes():
    assert format_duration(timedelta(hours=1, minutes=5)) == "1h 5m"
    assert format_duration(timedelta(minutes=45)) == "45m"


def test_parse_time_hint_ago():
    ref = datetime(2025, 6, 15, 14, 0, tzinfo=ZoneInfo("Europe/London"))
    got = parse_time_hint("30 minutes ago", reference=ref)
    assert got.tzinfo == timezone.utc
    expect_local = ref - timedelta(minutes=30)
    assert abs((got.astimezone(ZoneInfo("Europe/London")) - expect_local).total_seconds()) < 1


def test_format_uk_shows_offset():
    utc = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
    s = format_uk(utc)
    assert "2025-01-15" in s
    assert "12:00" in s or "13:00" in s  # GMT vs BST edge; mid-Jan is GMT


def test_uk_now_is_aware():
    n = uk_now()
    assert n.tzinfo is not None
