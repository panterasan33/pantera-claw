from app.bot.handlers import build_confirmation_keyboard
from app.services.classifier import ClassificationService, MessageType
from app.services.datetime_parser import parse_natural_date, parse_natural_datetime


def test_build_confirmation_keyboard_embeds_inbox_id():
    keyboard = build_confirmation_keyboard(MessageType.TASK, item_id=12, inbox_item_id=34)
    payloads = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "confirm:task:12:34" in payloads
    assert "change:reminder:task:12:34" in payloads


def test_classifier_extracts_fenced_json():
    service = ClassificationService()
    result = service._parse_response(
        """```json
        {"type":"task","confidence":0.91,"data":{"title":"Book MOT"}}
        ```"""
    )
    assert result.message_type == MessageType.TASK
    assert result.extracted_data["title"] == "Book MOT"


def test_parse_natural_date_supports_weekday():
    parsed = parse_natural_date("next monday")
    assert parsed is not None


def test_parse_natural_datetime_supports_relative_hours():
    parsed = parse_natural_datetime("in 2 hours")
    assert parsed is not None
