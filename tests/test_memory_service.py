from datetime import date

from app.services.memory_service import parse_event_date


def test_parse_event_date_today():
    assert parse_event_date("today") == date.today()


def test_parse_event_date_mm_dd_uses_current_year():
    parsed = parse_event_date("10/15")
    assert parsed is not None
    assert parsed.year == date.today().year
    assert parsed.month == 10
    assert parsed.day == 15


def test_parse_event_date_month_name():
    parsed = parse_event_date("October 15th")
    assert parsed is not None
    assert parsed.year == date.today().year
    assert parsed.month == 10
    assert parsed.day == 15
