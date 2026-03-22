"""Unit tests for Telegram + orchestrator integration helpers (no DB)."""

from app.bot.handlers import build_clarification_keyboard
from app.services.classifier import MessageType
from app.services.orchestrator import (
    CLARIFICATION_THRESHOLD,
    combine_clarification_context,
    exempt_from_clarification_gate,
)


def test_combine_clarification_context_appends_user_follow_up():
    merged = combine_clarification_context("Buy milk", "tomorrow morning")
    assert "Buy milk" in merged
    assert "tomorrow morning" in merged
    assert "additional context" in merged


def test_combine_clarification_context_strips_whitespace():
    assert combine_clarification_context("  x  ", "  ") == "x"


def test_clarification_threshold_is_documented_range():
    assert 0.5 < CLARIFICATION_THRESHOLD < 1.0


def test_exempt_from_clarification_gate():
    assert exempt_from_clarification_gate(MessageType.QUESTION)
    assert exempt_from_clarification_gate(MessageType.CONVERSATION)
    assert exempt_from_clarification_gate(MessageType.CORRECTION)
    assert exempt_from_clarification_gate(MessageType.UPDATE)
    assert not exempt_from_clarification_gate(MessageType.TASK)


def test_build_clarification_keyboard_callback_includes_inbox_id():
    kb = build_clarification_keyboard("task", "reminder", inbox_item_id=42)
    payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "clarify_task_42" in payloads
    assert "clarify_reminder_42" in payloads
