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
from app.services.llm_usage_service import record_from_anthropic_message, record_from_openai_chat
from app.services.task_service import create_task_from_classification
from app.services.emilia_nap_service import apply_emilia_nap_action

logger = logging.getLogger(__name__)

CLARIFICATION_THRESHOLD = 0.65


@dataclass
class ProcessingOutcome:
    classification: ClassificationResult
    inbox_item_id: int
    entity_type: Optional[str]
    entity_id: Optional[int]
    reply_text: str
    needs_confirmation: bool
    awaiting_clarification: bool = False


def combine_clarification_context(base: str, additional_user_text: str) -> str:
    """Merge original capture with follow-up text after a clarifying question."""
    a = (additional_user_text or "").strip()
    b = (base or "").strip()
    if not a:
        return b
    return f"{b}. (additional context: {a})"


def exempt_from_clarification_gate(message_type: MessageType) -> bool:
    return message_type in {
        MessageType.QUESTION,
        MessageType.CONVERSATION,
        MessageType.CORRECTION,
        MessageType.UPDATE,
        MessageType.EMILIA_NAP,
    }


async def generate_clarifying_question(text: str, result: ClassificationResult) -> str:
    """Ask the LLM to produce a single focused clarifying question."""
    from app.config import get_settings

    settings = get_settings()
    safe = (text or "").replace("{", "{{").replace("}", "}}")
    prompt = (
        f'A personal assistant received this message: "{safe}"\n'
        f"It was classified as '{result.message_type.value}' with {result.confidence:.0%} confidence.\n"
        "Generate ONE short clarifying question (max 15 words) to confirm the user's intent. "
        "For reminders ask about timing; for tasks ask about deadline or priority; "
        "for notes ask if action is needed. Reply with only the question."
    )

    try:
        if settings.anthropic_api_key:
            import anthropic as _anthropic

            client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            model_id = "claude-3-haiku-20240307"
            resp = await client.messages.create(
                model=model_id,
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            await record_from_anthropic_message(model=model_id, operation="clarification", response=resp)
            return resp.content[0].text.strip()
        if settings.openai_api_key:
            import openai as _openai

            client = _openai.AsyncOpenAI(api_key=settings.openai_api_key)
            model_id = "gpt-4o-mini"
            resp = await client.chat.completions.create(
                model=model_id,
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            await record_from_openai_chat(model=model_id, operation="clarification", response=resp)
            return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("Failed to generate clarifying question: %s", e)

    if result.message_type == MessageType.REMINDER:
        return "When should I remind you about this?"
    if result.message_type == MessageType.TASK:
        return "Is this something you need to do, or just a note to remember?"
    return "Did you want me to save this as a task or a note?"


async def _build_conversation_reply(text: str, history: list) -> str:
    """Generate a short reply for CONVERSATION classifications."""
    from app.config import get_settings

    settings = get_settings()
    history_lines = "\n".join(
        f"  [{t['role'].title()}]: {t['text'][:120]}" for t in history[-4:] if t.get("text")
    )
    prompt = (
        "You are Pantera, a smart personal assistant. Reply in 1-2 short sentences.\n"
        "If the user's message looks like it could be a task, reminder, or note, "
        "gently suggest saving it. Otherwise be friendly and direct.\n\n"
        f"Recent context:\n{history_lines}\n\n"
        f"User: {text}"
    )

    try:
        if settings.anthropic_api_key:
            import anthropic as _anthropic

            client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            model_id = "claude-3-haiku-20240307"
            resp = await client.messages.create(
                model=model_id,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            await record_from_anthropic_message(model=model_id, operation="conversation_reply", response=resp)
            return resp.content[0].text.strip()
        if settings.openai_api_key:
            import openai as _openai

            client = _openai.AsyncOpenAI(api_key=settings.openai_api_key)
            model_id = "gpt-4o-mini"
            resp = await client.chat.completions.create(
                model=model_id,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            await record_from_openai_chat(model=model_id, operation="conversation_reply", response=resp)
            return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("CONVERSATION LLM failed: %s", e)

    return "👋 I'm here! Send me tasks, reminders, or notes and I'll keep everything organized."


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
    if message_type == MessageType.EMILIA_NAP:
        return {"action": "status", "time_hint": None, "notes": None}
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


async def _run_inbox_pipeline(
    inbox_item_id: int,
    *,
    text: str,
    source_type: str,
    telegram_message_id: Optional[int],
    telegram_file_id: Optional[str],
    conversation_history: Optional[list] = None,
    classification: Optional[ClassificationResult] = None,
    chat_id: Optional[int] = None,
) -> ProcessingOutcome:
    """Classify, optionally persist entity, update inbox row, log ROUTED (or clarification)."""
    classifier = get_classifier()
    classification = classification or await classifier.classify(text, conversation_history=conversation_history or [])

    if classification.message_type == MessageType.CONVERSATION:
        reply_text = await _build_conversation_reply(text, conversation_history or [])
        await _update_inbox_item(
            inbox_item_id,
            classification=classification.message_type.value,
            confidence=classification.confidence,
            extracted_data=dict(classification.extracted_data or {}),
            is_processed=True,
            needs_clarification=False,
            processed_content=text,
        )
        await _log_interaction_event(
            event_type=InteractionEventType.ROUTED,
            inbox_item_id=inbox_item_id,
            source_type=classification.message_type.value,
            source_entity_id=None,
            target_type=None,
            target_entity_id=None,
            summary=_build_event_summary(
                InteractionEventType.ROUTED,
                message_type=classification.message_type.value,
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
            inbox_item_id=inbox_item_id,
            entity_type=None,
            entity_id=None,
            reply_text=reply_text,
            needs_confirmation=False,
            awaiting_clarification=False,
        )

    if classification.message_type == MessageType.EMILIA_NAP:
        data = classification.extracted_data or {}
        if chat_id is None:
            reply_text = "🍼 I need an active chat to log Emilia's naps."
            entity_type, entity_id = None, None
        else:
            entity_type = "emilia_nap"
            try:
                entity_id, reply_text = await apply_emilia_nap_action(
                    chat_id=chat_id,
                    action=(data.get("action") or "status"),
                    time_hint=data.get("time_hint"),
                    notes=data.get("notes"),
                    raw_text=text,
                    telegram_message_id=telegram_message_id,
                )
            except Exception:
                logger.exception("Emilia nap pipeline failed for chat_id=%s", chat_id)
                entity_id = None
                reply_text = (
                    "🍼 I hit an error saving the nap log (often a missing `emilia_naps` table). "
                    "Restart Pantera once so the database can create new tables, then try again."
                )
        extracted_data = dict(data)
        if entity_type and entity_id is not None:
            extracted_data["routed_entity"] = {"type": entity_type, "id": entity_id}
        await _update_inbox_item(
            inbox_item_id,
            classification=classification.message_type.value,
            confidence=classification.confidence,
            extracted_data=extracted_data,
            is_processed=True,
            needs_clarification=False,
            processed_content=text,
        )
        await _log_interaction_event(
            event_type=InteractionEventType.ROUTED,
            inbox_item_id=inbox_item_id,
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
            inbox_item_id=inbox_item_id,
            entity_type=entity_type,
            entity_id=entity_id,
            reply_text=reply_text,
            needs_confirmation=False,
            awaiting_clarification=False,
        )

    if (
        classification.confidence < CLARIFICATION_THRESHOLD
        and not exempt_from_clarification_gate(classification.message_type)
    ):
        question = await generate_clarifying_question(text, classification)
        await _update_inbox_item(
            inbox_item_id,
            classification=classification.message_type.value,
            confidence=classification.confidence,
            extracted_data=dict(classification.extracted_data or {}),
            is_processed=False,
            needs_clarification=True,
            processed_content=text,
        )
        return ProcessingOutcome(
            classification=classification,
            inbox_item_id=inbox_item_id,
            entity_type=None,
            entity_id=None,
            reply_text=question,
            needs_confirmation=False,
            awaiting_clarification=True,
        )

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
        inbox_item_id,
        classification=classification.message_type.value,
        confidence=classification.confidence,
        extracted_data=extracted_data,
        is_processed=True,
        needs_clarification=classification.confidence < 0.9,
        processed_content=text,
    )

    await _log_interaction_event(
        event_type=InteractionEventType.ROUTED,
        inbox_item_id=inbox_item_id,
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
        inbox_item_id=inbox_item_id,
        entity_type=entity_type,
        entity_id=entity_id,
        reply_text=reply_text,
        needs_confirmation=classification.confidence < 0.9,
        awaiting_clarification=False,
    )


async def resume_inbox_after_clarification(
    *,
    inbox_item_id: int,
    additional_user_text: str,
    telegram_message_id: int,
    conversation_history: Optional[list] = None,
    chat_id: Optional[int] = None,
) -> ProcessingOutcome:
    """Continue processing after user answered a low-confidence clarifying question."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(select(InboxItem).where(InboxItem.id == inbox_item_id))
        inbox = r.scalar_one_or_none()
    if not inbox:
        raise ValueError("Inbox item not found")
    combined = combine_clarification_context(
        inbox.processed_content or inbox.raw_content,
        additional_user_text,
    )
    return await _run_inbox_pipeline(
        inbox_item_id,
        text=combined.strip(),
        source_type=inbox.source_type,
        telegram_message_id=telegram_message_id,
        telegram_file_id=inbox.telegram_file_id,
        conversation_history=conversation_history,
        classification=None,
        chat_id=chat_id,
    )


async def apply_clarification_choice(
    *,
    inbox_item_id: int,
    chosen_type: MessageType,
    telegram_message_id: Optional[int],
    chat_id: Optional[int] = None,
) -> ProcessingOutcome:
    """After user taps clarify_task / clarify_reminder, persist using the chosen type."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(select(InboxItem).where(InboxItem.id == inbox_item_id))
        inbox = r.scalar_one_or_none()
    if not inbox:
        raise ValueError("Inbox item not found")
    base = (inbox.processed_content or inbox.raw_content or "").strip()
    classifier = get_classifier()
    forced = await classifier.classify(f"{chosen_type.value}: {base}")
    forced = ClassificationResult(
        message_type=chosen_type,
        confidence=1.0,
        extracted_data=dict(forced.extracted_data or {}),
    )
    return await _run_inbox_pipeline(
        inbox_item_id,
        text=base,
        source_type=inbox.source_type,
        telegram_message_id=telegram_message_id,
        telegram_file_id=inbox.telegram_file_id,
        conversation_history=None,
        classification=forced,
        chat_id=chat_id,
    )


async def process_incoming_content(
    *,
    raw_content: str,
    processed_content: Optional[str] = None,
    source_type: str = "text",
    telegram_message_id: Optional[int] = None,
    telegram_file_id: Optional[str] = None,
    conversation_history: Optional[list] = None,
    classification: Optional[ClassificationResult] = None,
) -> ProcessingOutcome:
    text = (processed_content or raw_content or "").strip()
    inbox_item = await _create_inbox_item(
        raw_content=raw_content or text,
        processed_content=processed_content,
        source_type=source_type,
        telegram_message_id=telegram_message_id,
        telegram_file_id=telegram_file_id,
    )

    try:
        return await _run_inbox_pipeline(
            inbox_item.id,
            text=text,
            source_type=source_type,
            telegram_message_id=telegram_message_id,
            telegram_file_id=telegram_file_id,
            conversation_history=conversation_history,
            classification=classification,
            chat_id=chat_id,
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
    chat_id: Optional[int] = None,
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
        chat_id=chat_id,
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
        MessageType.EMILIA_NAP: "🍼 *Reclassified as Emilia nap log.*",
    }.get(target_type, "✅ *Reclassified.*")
    return new_entity_type, new_entity_id, reply
