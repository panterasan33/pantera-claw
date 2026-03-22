"""Lightweight intake orchestrator for classification, routing, and feedback memory."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models.inbox import InboxItem
from app.models.interaction_event import InteractionEvent, InteractionEventType
from app.models.memory import MemoryItem, MemoryType
from app.models.reminder import Reminder
from app.models.task import Task
from app.services.classifier import ClassificationResult, MessageType, get_classifier
from app.services.embedding_service import embed_text
from app.services.memory_service import create_memory_from_classification
from app.services.reminder_service import create_reminder_from_classification
from app.services.search_service import answer_question
from app.services.task_service import create_task_from_classification

logger = logging.getLogger(__name__)


@dataclass
class ProcessingOutcome:
    classification: ClassificationResult
    inbox_item_id: int
    entity_type: Optional[str]
    entity_id: Optional[int]
    reply_text: str
    needs_confirmation: bool


def _build_event_summary(
    event_type: InteractionEventType,
    *,
    message_type: Optional[str] = None,
    target_type: Optional[str] = None,
    content: Optional[str] = None,
) -> str:
    snippet = (content or "").strip().replace("\n", " ")
    if len(snippet) > 120:
        snippet = f"{snippet[:117]}..."

    if event_type == InteractionEventType.INGESTED:
        return f"Ingested {message_type or 'input'}: {snippet}".strip()
    if event_type == InteractionEventType.ROUTED:
        return f"Routed {message_type or 'input'} into {target_type or 'storage'}: {snippet}".strip()
    if event_type == InteractionEventType.CONFIRMED:
        return f"User confirmed {message_type or 'classification'} for: {snippet}".strip()
    if event_type == InteractionEventType.RECLASSIFIED:
        return f"User reclassified {message_type or 'item'} to {target_type or 'item'}: {snippet}".strip()
    if event_type == InteractionEventType.EDIT_REQUESTED:
        return f"User requested an edit for {message_type or 'item'}: {snippet}".strip()
    if event_type == InteractionEventType.EDIT_APPLIED:
        return f"User edit applied to {message_type or 'item'}: {snippet}".strip()
    return f"Processing error for {message_type or 'item'}: {snippet}".strip()


async def _log_interaction_event(
    *,
    event_type: InteractionEventType,
    inbox_item_id: Optional[int],
    source_type: Optional[str],
    source_entity_id: Optional[int],
    target_type: Optional[str],
    target_entity_id: Optional[int],
    summary: str,
    details: Optional[dict] = None,
) -> None:
    async with AsyncSessionLocal() as session:
        event = InteractionEvent(
            event_type=event_type,
            inbox_item_id=inbox_item_id,
            source_type=source_type,
            source_entity_id=source_entity_id,
            target_type=target_type,
            target_entity_id=target_entity_id,
            summary=summary,
            details=details,
        )
        session.add(event)
        await session.flush()
        try:
            emb = await embed_text(summary)
            if emb:
                event.embedding = emb
        except Exception:
            logger.debug("Failed to embed interaction event", exc_info=True)
        await session.commit()


async def _create_inbox_item(
    *,
    raw_content: str,
    processed_content: Optional[str],
    source_type: str,
    telegram_message_id: Optional[int],
    telegram_file_id: Optional[str],
) -> InboxItem:
    async with AsyncSessionLocal() as session:
        item = InboxItem(
            raw_content=raw_content,
            processed_content=processed_content,
            source_type=source_type,
            telegram_message_id=telegram_message_id,
            telegram_file_id=telegram_file_id,
            is_processed=False,
            needs_clarification=False,
        )
        session.add(item)
        await session.flush()
        try:
            emb = await embed_text(processed_content or raw_content)
            if emb:
                item.embedding = emb
        except Exception:
            logger.debug("Failed to embed inbox item", exc_info=True)
        await session.commit()
        await session.refresh(item)

    await _log_interaction_event(
        event_type=InteractionEventType.INGESTED,
        inbox_item_id=item.id,
        source_type=source_type,
        source_entity_id=None,
        target_type=None,
        target_entity_id=None,
        summary=_build_event_summary(
            InteractionEventType.INGESTED,
            message_type=source_type,
            content=processed_content or raw_content,
        ),
        details={
            "raw_content": raw_content,
            "processed_content": processed_content,
            "telegram_message_id": telegram_message_id,
            "telegram_file_id": telegram_file_id,
        },
    )
    return item


async def _update_inbox_item(
    inbox_item_id: int,
    *,
    classification: Optional[str],
    confidence: Optional[float],
    extracted_data: Optional[dict],
    is_processed: bool,
    needs_clarification: bool,
    processed_content: Optional[str] = None,
) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(InboxItem).where(InboxItem.id == inbox_item_id))
        item = result.scalar_one_or_none()
        if not item:
            return
        item.classification = classification
        item.classification_confidence = confidence
        item.extracted_data = extracted_data
        item.is_processed = is_processed
        item.needs_clarification = needs_clarification
        if processed_content is not None:
            item.processed_content = processed_content
        await session.commit()


async def _replace_entity(
    *,
    source_type: str,
    source_entity_id: int,
    target_classification: MessageType,
    text: str,
    telegram_message_id: Optional[int],
) -> tuple[Optional[str], Optional[int]]:
    target_type, target_id = await _persist_classified_entity(
        classification=ClassificationResult(
            message_type=target_classification,
            confidence=1.0,
            extracted_data=_manual_extracted_data(target_classification, text),
        ),
        text=text,
        telegram_message_id=telegram_message_id,
    )

    async with AsyncSessionLocal() as session:
        model = {"task": Task, "reminder": Reminder, "memory": MemoryItem}.get(source_type)
        if model:
            result = await session.execute(select(model).where(model.id == source_entity_id))
            entity = result.scalar_one_or_none()
            if entity:
                await session.delete(entity)
                await session.commit()
    return target_type, target_id


def _manual_extracted_data(message_type: MessageType, text: str) -> dict:
    if message_type == MessageType.TASK:
        return {"title": text}
    if message_type == MessageType.REMINDER:
        return {"content": text, "trigger_time": None, "is_recurring": False}
    if message_type in {MessageType.MEMORY, MessageType.NOTE, MessageType.DISCLOSURE}:
        return {"content": text, "memory_subtype": "note"}
    if message_type == MessageType.QUESTION:
        return {"query": text}
    return {"content": text}


async def _persist_classified_entity(
    *,
    classification: ClassificationResult,
    text: str,
    telegram_message_id: Optional[int],
) -> tuple[Optional[str], Optional[int]]:
    data = classification.extracted_data

    if classification.message_type == MessageType.TASK:
        task_id = await create_task_from_classification(
            title=data.get("title", text[:200]),
            notes=data.get("notes"),
            due_date_str=data.get("due_date"),
            project=data.get("project"),
            group=data.get("group"),
            telegram_message_id=telegram_message_id,
        )
        return "task", task_id

    if classification.message_type == MessageType.REMINDER:
        reminder_id = await create_reminder_from_classification(
            content=data.get("content", text[:200]),
            trigger_time=data.get("trigger_time"),
            is_recurring=data.get("is_recurring", False),
            recurrence_pattern=data.get("recurrence_pattern"),
            recurrence_config=data.get("recurrence_config"),
            telegram_message_id=telegram_message_id,
        )
        return "reminder", reminder_id

    if classification.message_type == MessageType.MEMORY:
        event_date = data.get("event_date")
        memory_id = await create_memory_from_classification(
            content=data.get("content", text[:200]),
            event_date=event_date,
            is_annual=data.get("is_annual", False),
            memory_subtype=data.get("memory_subtype"),
            source_type="telegram",
            telegram_message_id=telegram_message_id,
        )
        if event_date and (data.get("is_annual") or data.get("memory_subtype") in {"birthday", "annual_event"}):
            await create_reminder_from_classification(
                content=data.get("content", text[:200]),
                trigger_time=event_date,
                is_recurring=True,
                recurrence_pattern="yearly",
                recurrence_config={"source": "memory", "memory_id": memory_id},
                telegram_message_id=telegram_message_id,
            )
        return "memory", memory_id

    if classification.message_type == MessageType.NOTE:
        memory_id = await create_memory_from_classification(
            content=data.get("content", text[:200]),
            memory_subtype="note",
            tags=data.get("tags"),
            original_message=text,
            source_type="telegram",
            telegram_message_id=telegram_message_id,
        )
        return "memory", memory_id

    if classification.message_type == MessageType.DISCLOSURE:
        memory_id = await create_memory_from_classification(
            content=data.get("summary", text[:200]),
            memory_subtype="note",
            summary=data.get("summary"),
            original_message=text,
            source_type="telegram",
            telegram_message_id=telegram_message_id,
        )
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(MemoryItem).where(MemoryItem.id == memory_id))
            item = result.scalar_one_or_none()
            if item:
                item.memory_type = MemoryType.DISCLOSURE
                await session.commit()
        return "memory", memory_id

    return None, None


async def build_capture_response(
    classification: ClassificationResult,
    text: str,
    *,
    question_answer: Optional[str] = None,
) -> str:
    data = classification.extracted_data

    if classification.message_type == MessageType.TASK:
        response = f"📋 *Task captured:* {data.get('title', text[:50])}"
        if data.get("due_date"):
            response += f"\n📅 Due: {data['due_date']}"
        if data.get("project"):
            response += f"\n📁 Project: {data['project']}"
        return response

    if classification.message_type == MessageType.REMINDER:
        response = f"🔔 *Reminder set:* {data.get('content', text[:50])}"
        response += f"\n⏰ {data.get('trigger_time') or 'Time to be clarified'}"
        if data.get("is_recurring"):
            pattern = data.get("recurrence_detail") or data.get("recurrence_pattern") or "recurring"
            response += f"\n🔁 Recurring: {pattern}"
        return response

    if classification.message_type == MessageType.MEMORY:
        response = f"🧠 *Remembered:* {data.get('content', text[:50])}"
        if data.get("memory_subtype") == "birthday":
            response += "\n🎂 I'll remind you before the day"
        else:
            response += "\n📆 I'll surface this at the right time"
        return response

    if classification.message_type == MessageType.NOTE:
        response = f"📝 *Note saved:* {data.get('content', text[:100])[:100]}"
        tags = data.get("tags") or []
        if tags:
            response += f"\n🏷️ Tags: {', '.join(tags)}"
        return response

    if classification.message_type == MessageType.DISCLOSURE:
        return "💭 *Noted.* I'll remember this for future context."

    if classification.message_type == MessageType.QUESTION:
        return question_answer or "🔍 I couldn't assemble an answer yet."

    return "👋 How can I help you organize something?"


async def process_incoming_content(
    *,
    raw_content: str,
    processed_content: Optional[str] = None,
    source_type: str = "text",
    telegram_message_id: Optional[int] = None,
    telegram_file_id: Optional[str] = None,
) -> ProcessingOutcome:
    text = (processed_content or raw_content or "").strip()
    inbox_item = await _create_inbox_item(
        raw_content=raw_content or text,
        processed_content=processed_content,
        source_type=source_type,
        telegram_message_id=telegram_message_id,
        telegram_file_id=telegram_file_id,
    )

    classifier = get_classifier()
    try:
        classification = await classifier.classify(text)
        entity_type, entity_id = await _persist_classified_entity(
            classification=classification,
            text=text,
            telegram_message_id=telegram_message_id,
        )
        answer = None
        if classification.message_type == MessageType.QUESTION:
            async with AsyncSessionLocal() as session:
                answer = await answer_question(session, classification.extracted_data.get("query", text))
        reply_text = await build_capture_response(classification, text, question_answer=answer)

        extracted_data = dict(classification.extracted_data or {})
        if entity_type and entity_id:
            extracted_data["routed_entity"] = {"type": entity_type, "id": entity_id}

        await _update_inbox_item(
            inbox_item.id,
            classification=classification.message_type.value,
            confidence=classification.confidence,
            extracted_data=extracted_data,
            is_processed=True,
            needs_clarification=classification.confidence < 0.9,
            processed_content=processed_content or text,
        )

        await _log_interaction_event(
            event_type=InteractionEventType.ROUTED,
            inbox_item_id=inbox_item.id,
            source_type=classification.message_type.value,
            source_entity_id=entity_id,
            target_type=entity_type,
            target_entity_id=entity_id,
            summary=_build_event_summary(
                InteractionEventType.ROUTED,
                message_type=classification.message_type.value,
                target_type=entity_type,
                content=text,
            ),
            details={
                "classification_confidence": classification.confidence,
                "extracted_data": classification.extracted_data,
                "source_type": source_type,
            },
        )
        return ProcessingOutcome(
            classification=classification,
            inbox_item_id=inbox_item.id,
            entity_type=entity_type,
            entity_id=entity_id,
            reply_text=reply_text,
            needs_confirmation=classification.confidence < 0.9,
        )
    except Exception as exc:
        logger.exception("Processing failed for inbox item %s", inbox_item.id)
        await _update_inbox_item(
            inbox_item.id,
            classification=None,
            confidence=None,
            extracted_data={"error": str(exc)},
            is_processed=False,
            needs_clarification=True,
            processed_content=processed_content or text,
        )
        await _log_interaction_event(
            event_type=InteractionEventType.PROCESSING_ERROR,
            inbox_item_id=inbox_item.id,
            source_type=source_type,
            source_entity_id=None,
            target_type=None,
            target_entity_id=None,
            summary=_build_event_summary(
                InteractionEventType.PROCESSING_ERROR,
                message_type=source_type,
                content=text,
            ),
            details={"error": str(exc)},
        )
        raise


async def log_unprocessed_input(
    *,
    raw_content: str,
    processed_content: Optional[str] = None,
    source_type: str = "text",
    telegram_message_id: Optional[int] = None,
    telegram_file_id: Optional[str] = None,
) -> int:
    """Persist input to the inbox ledger without classifying it yet."""
    inbox_item = await _create_inbox_item(
        raw_content=raw_content,
        processed_content=processed_content,
        source_type=source_type,
        telegram_message_id=telegram_message_id,
        telegram_file_id=telegram_file_id,
    )
    await _update_inbox_item(
        inbox_item.id,
        classification=None,
        confidence=None,
        extracted_data={"status": "queued_for_manual_processing"},
        is_processed=False,
        needs_clarification=True,
        processed_content=processed_content,
    )
    return inbox_item.id


async def confirm_classification(
    *,
    inbox_item_id: int,
    classification: str,
    entity_type: Optional[str],
    entity_id: Optional[int],
) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(InboxItem).where(InboxItem.id == inbox_item_id))
        item = result.scalar_one_or_none()
        if item:
            item.needs_clarification = False
            await session.commit()
            content = item.processed_content or item.raw_content
        else:
            content = ""

    await _log_interaction_event(
        event_type=InteractionEventType.CONFIRMED,
        inbox_item_id=inbox_item_id,
        source_type=classification,
        source_entity_id=entity_id,
        target_type=entity_type,
        target_entity_id=entity_id,
        summary=_build_event_summary(
            InteractionEventType.CONFIRMED,
            message_type=classification,
            content=content,
        ),
        details={"classification": classification},
    )


async def request_edit(
    *,
    inbox_item_id: int,
    source_type: str,
    source_entity_id: Optional[int],
) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(InboxItem).where(InboxItem.id == inbox_item_id))
        item = result.scalar_one_or_none()
        if item:
            item.needs_clarification = True
            await session.commit()
            content = item.processed_content or item.raw_content
        else:
            content = ""

    await _log_interaction_event(
        event_type=InteractionEventType.EDIT_REQUESTED,
        inbox_item_id=inbox_item_id,
        source_type=source_type,
        source_entity_id=source_entity_id,
        target_type=None,
        target_entity_id=None,
        summary=_build_event_summary(
            InteractionEventType.EDIT_REQUESTED,
            message_type=source_type,
            content=content,
        ),
    )


async def apply_edit(
    *,
    inbox_item_id: int,
    source_type: str,
    source_entity_id: Optional[int],
    edited_text: str,
    telegram_message_id: Optional[int],
) -> ProcessingOutcome:
    target_type = source_type
    target_id = source_entity_id
    if source_type in {"task", "reminder", "memory"} and source_entity_id:
        async with AsyncSessionLocal() as session:
            model = {"task": Task, "reminder": Reminder, "memory": MemoryItem}[source_type]
            result = await session.execute(select(model).where(model.id == source_entity_id))
            entity = result.scalar_one_or_none()
            if entity:
                await session.delete(entity)
                await session.commit()

    await _log_interaction_event(
        event_type=InteractionEventType.EDIT_APPLIED,
        inbox_item_id=inbox_item_id,
        source_type=source_type,
        source_entity_id=source_entity_id,
        target_type=target_type,
        target_entity_id=target_id,
        summary=_build_event_summary(
            InteractionEventType.EDIT_APPLIED,
            message_type=source_type,
            content=edited_text,
        ),
        details={"edited_text": edited_text},
    )

    return await process_incoming_content(
        raw_content=edited_text,
        processed_content=edited_text,
        source_type="edit",
        telegram_message_id=telegram_message_id,
    )


async def reclassify_inbox_item(
    *,
    inbox_item_id: int,
    source_type: str,
    source_entity_id: Optional[int],
    target_type: MessageType,
    telegram_message_id: Optional[int],
) -> tuple[Optional[str], Optional[int], str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(InboxItem).where(InboxItem.id == inbox_item_id))
        item = result.scalar_one_or_none()
        if not item:
            raise ValueError("Inbox item not found")
        text = item.processed_content or item.raw_content

    new_entity_type, new_entity_id = await _replace_entity(
        source_type=source_type,
        source_entity_id=source_entity_id or 0,
        target_classification=target_type,
        text=text,
        telegram_message_id=telegram_message_id,
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(InboxItem).where(InboxItem.id == inbox_item_id))
        item = result.scalar_one_or_none()
        if item:
            extracted = dict(item.extracted_data or {})
            extracted["corrected_to"] = target_type.value
            extracted["routed_entity"] = {"type": new_entity_type, "id": new_entity_id}
            item.classification = target_type.value
            item.classification_confidence = 1.0
            item.extracted_data = extracted
            item.needs_clarification = False
            item.is_processed = True
            await session.commit()

    await _log_interaction_event(
        event_type=InteractionEventType.RECLASSIFIED,
        inbox_item_id=inbox_item_id,
        source_type=source_type,
        source_entity_id=source_entity_id,
        target_type=new_entity_type or target_type.value,
        target_entity_id=new_entity_id,
        summary=_build_event_summary(
            InteractionEventType.RECLASSIFIED,
            message_type=source_type,
            target_type=target_type.value,
            content=text,
        ),
        details={
            "from": source_type,
            "to": target_type.value,
            "new_entity_type": new_entity_type,
            "new_entity_id": new_entity_id,
        },
    )

    reply = {
        MessageType.TASK: "📋 *Reclassified as task.* Got it.",
        MessageType.REMINDER: "🔔 *Reclassified as reminder.* I saved it as a reminder.",
        MessageType.NOTE: "📝 *Reclassified as note.* Saved to memory.",
        MessageType.MEMORY: "🧠 *Reclassified as memory.* I'll keep this in long-term memory.",
    }.get(target_type, "✅ *Reclassified.*")
    return new_entity_type, new_entity_id, reply
