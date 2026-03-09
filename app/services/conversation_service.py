"""
Conversation history helpers.

Provides a thin persistence layer over ConversationMessage so that:
- Recent turns are available to the classifier as context
- Callback metadata survives bot restarts
- Pending clarifications can be detected before processing the next message
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select, update

from app.db.database import AsyncSessionLocal
from app.models.conversation import ConversationMessage


async def save_message(
    chat_id: int,
    role: str,
    text: str,
    *,
    telegram_message_id: Optional[int] = None,
    item_id: Optional[int] = None,
    item_type: Optional[str] = None,
    classification_type: Optional[str] = None,
    classification_confidence: Optional[float] = None,
    pending_clarification: bool = False,
    pending_inbox_item_id: Optional[int] = None,
) -> ConversationMessage:
    """Persist one turn of the conversation and return the saved row."""
    async with AsyncSessionLocal() as session:
        msg = ConversationMessage(
            chat_id=chat_id,
            role=role,
            text=text,
            telegram_message_id=telegram_message_id,
            item_id=item_id,
            item_type=item_type,
            classification_type=classification_type,
            classification_confidence=classification_confidence,
            pending_clarification=pending_clarification,
            pending_inbox_item_id=pending_inbox_item_id,
        )
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        return msg


async def get_recent_history(chat_id: int, limit: int = 6) -> list[dict]:
    """Return the last *limit* messages for a chat as plain dicts."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.chat_id == chat_id)
            .order_by(ConversationMessage.created_at.desc())
            .limit(limit)
        )
        rows = result.scalars().all()

    # Return in chronological order (oldest first)
    return [
        {
            "role": m.role,
            "text": m.text,
            "item_id": m.item_id,
            "item_type": m.item_type,
            "classification_type": m.classification_type,
        }
        for m in reversed(rows)
    ]


async def get_last_captured_item(chat_id: int) -> Optional[dict]:
    """Return the most recent user message that produced a captured item."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(
                ConversationMessage.chat_id == chat_id,
                ConversationMessage.role == "user",
                ConversationMessage.item_id.isnot(None),
            )
            .order_by(ConversationMessage.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()

    if row is None:
        return None
    return {
        "item_id": row.item_id,
        "item_type": row.item_type,
        "text": row.text,
        "classification_type": row.classification_type,
    }


async def get_pending_clarification(chat_id: int) -> Optional[ConversationMessage]:
    """Return the most recent bot message that is awaiting a clarification answer."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(
                ConversationMessage.chat_id == chat_id,
                ConversationMessage.role == "bot",
                ConversationMessage.pending_clarification == True,
            )
            .order_by(ConversationMessage.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def resolve_pending_clarification(message_id: int) -> None:
    """Mark a pending-clarification bot message as resolved."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ConversationMessage)
            .where(ConversationMessage.id == message_id)
            .values(pending_clarification=False)
        )
        await session.commit()


async def get_context_for_item(item_id: int) -> Optional[dict]:
    """
    Fallback for handle_callback: look up classification context by item_id.
    Used when the in-memory CLASSIFICATION_CONTEXT dict has been cleared (restart).
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(
                ConversationMessage.item_id == item_id,
                ConversationMessage.role == "user",
            )
            .order_by(ConversationMessage.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()

    if row is None:
        return None
    return {
        "text": row.text,
        "predicted_type": row.classification_type,
        "confidence": row.classification_confidence or 0.0,
    }
