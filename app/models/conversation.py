"""
Conversation history model — persists per-chat message context.
Enables context-aware classification and survives bot restarts.
"""
from typing import Optional
from sqlalchemy import String, Text, Float, Integer, Boolean, BigInteger
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class ConversationMessage(Base, TimestampMixin):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Telegram chat id (bigint to cover group/channel ids)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)

    # "user" | "bot"
    role: Mapped[str] = mapped_column(String(10))

    # Full message / response text
    text: Mapped[str] = mapped_column(Text)

    # Telegram message id for source reference
    telegram_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # If this user message produced a captured item, track it here
    item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    item_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Classification metadata for the user turn
    classification_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    classification_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # When True, the bot has asked a clarifying question and is waiting for the answer.
    # The associated inbox_item_id holds the not-yet-persisted original message.
    pending_clarification: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # id of the InboxItem that needs clarification (only set when pending_clarification=True)
    pending_inbox_item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<ConversationMessage {self.id}: {self.role} chat={self.chat_id}>"
