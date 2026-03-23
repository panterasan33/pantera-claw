"""Unit tests for Emilia nap helpers (no database)."""
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from app.services.classifier import ClassificationService, MessageType
from app.services.emilia_nap_service import (
    extract_emilia_end_time_hint,
    extract_emilia_start_time_hint,
    format_duration,
    format_uk,
    parse_time_hint,
    uk_now,
)


def test_format_duration_hours_minutes():
    assert format_duration(timedelta(hours=1, minutes=5)) == "1h 5m"
    assert format_duration(timedelta(minutes=45)) == "45m"


def test_parse_time_hint_ago():
    ref = datetime(2025, 6, 15, 14, 0, tzinfo=ZoneInfo("Europe/London"))
    got = parse_time_hint("30 minutes ago", reference=ref)
    assert got.tzinfo == timezone.utc
    expect_local = ref - timedelta(minutes=30)
    assert abs((got.astimezone(ZoneInfo("Europe/London")) - expect_local).total_seconds()) < 1


def test_parse_time_hint_time_only():
    # "Time only" should be interpreted as "today at that wall time" (UK local).
    ref = datetime(2025, 6, 15, 14, 0, tzinfo=ZoneInfo("Europe/London"))  # BST (UTC+1)
    got = parse_time_hint("7:46 am", reference=ref)
    expect_utc = datetime(2025, 6, 15, 6, 46, tzinfo=timezone.utc)
    assert abs((got - expect_utc).total_seconds()) < 1


def test_format_uk_shows_offset():
    utc = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
    s = format_uk(utc)
    assert "2025-01-15" in s
    assert "12:00" in s or "13:00" in s  # GMT vs BST edge; mid-Jan is GMT


def test_uk_now_is_aware():
    n = uk_now()
    assert n.tzinfo is not None


def test_classifier_recognizes_emilias_possessive_and_apostrophe():
    c = ClassificationService()
    for msg in (
        "emilias nap started",
        "Emilia's down for a nap",
        "emi nap log",
        "Goob down for a nap",
        "GOOB nap log",
        "goob's asleep",
    ):
        r = c._classify_rules(msg)
        assert r.message_type == MessageType.EMILIA_NAP, msg


def test_classifier_goob_explicit_prefix():
    c = ClassificationService()
    r = c._classify_rules("goob: how long asleep")
    assert r.message_type == MessageType.EMILIA_NAP


def test_classifier_nap_followup_uses_history():
    c = ClassificationService()
    hist = [
        {"role": "user", "text": "Goob went down for nap at 2", "item_type": "emilia_nap"},
        {"role": "bot", "text": "Nap started"},
    ]
    r = c._classify_rules("how long has she been asleep?", hist)
    assert r.message_type == MessageType.EMILIA_NAP


def test_extract_emilia_start_and_end_time_hints():
    msg = "Emilia fell asleep at 7:46 am and woke up at 8:32 am"
    assert extract_emilia_start_time_hint(msg) == "7:46 am"
    assert extract_emilia_end_time_hint(msg) == "8:32 am"


def test_classifier_emilia_wake_question_maps_to_status():
    c = ClassificationService()
    r = c._classify_rules("When did Emilia wake up?")
    assert r.message_type == MessageType.EMILIA_NAP
    assert r.extracted_data.get("action") == "status"
