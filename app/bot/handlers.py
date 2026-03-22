"""Telegram bot message handlers."""
import base64
import logging
import os
import tempfile
from datetime import datetime, timedelta

from openai import AsyncOpenAI
from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.config import get_settings
from app.db.database import AsyncSessionLocal
from app.models.classification_feedback import ClassificationFeedback
from app.models.inbox import InboxItem
from app.models.reminder import Reminder, ReminderType
from app.models.task import Task
from app.services.classifier import ClassificationResult, MessageType, get_classifier
from app.services.classifier_learning import get_learning_service
from app.services.llm_usage_service import record_from_openai_chat, record_whisper_call
from app.services.conversation_service import (
    get_context_for_item,
    get_last_captured_item,
    get_pending_clarification,
    get_recent_history,
    resolve_pending_clarification,
    save_message,
)
from app.services.orchestrator import (
    ProcessingOutcome,
    apply_clarification_choice,
    apply_edit,
    confirm_classification,
    log_unprocessed_input,
    process_incoming_content,
    reclassify_inbox_item,
    request_edit,
    resume_inbox_after_clarification,
)
from app.services.reminder_service import create_reminder_from_classification, parse_trigger_time
from app.services.search_service import semantic_search
from app.services.task_service import create_task_from_classification, parse_due_date

logger = logging.getLogger(__name__)

# Callback-driven reclassification cache (complements conversation_messages on restart).
CLASSIFICATION_CONTEXT: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        "🐆 *Pantera here!*\n\n"
        "I'm your personal assistant. Send me:\n"
        "• Tasks and to-dos\n"
        "• Reminders (one-off or recurring)\n"
        "• Notes and things to remember\n"
        "• Voice notes\n"
        "• Screenshots and images\n\n"
        "I'll capture, classify, and organize everything.\n"
        "Ask me anything about what you've shared with me.",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(
        "🐆 *Pantera Commands*\n\n"
        "*Capture:*\n"
        "Just send me text, voice, images, or documents - I'll log and organize them.\n\n"
        "*Commands:*\n"
        "/tasks - Show your tasks\n"
        "/today - Show My Day\n"
        "/reminders - Active reminders\n"
        "/search [query] - Search everything\n"
        "/projects - List projects\n\n"
        "*Quick actions:*\n"
        "• 'done [task]' - Mark complete\n"
        "• 'add subtask to [task]' - Add subtask\n"
        "• 'what's on my day?' - Daily view\n"
        "• 'remind me...' - Create reminder\n"
        "• 'actually make that a reminder' - Correct last item\n"
        "• 'change the date to Friday' - Update last item\n",
        parse_mode="Markdown",
    )


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tasks command."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.parent_id.is_(None)).order_by(Task.created_at.desc()).limit(20)
        )
        tasks = result.scalars().all()
    if not tasks:
        await update.message.reply_text("📋 No tasks yet.")
        return
    lines = ["📋 *Your tasks:*"]
    for task in tasks:
        lines.append(f"• {task.title}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /today command."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.my_day == True).order_by(Task.created_at.desc()).limit(20)
        )
        tasks = result.scalars().all()
    if not tasks:
        await update.message.reply_text("☀️ Nothing in My Day yet.")
        return
    lines = ["☀️ *My Day:*"]
    for task in tasks:
        lines.append(f"• {task.title}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /reminders command."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.is_active == True).order_by(Reminder.next_trigger.asc()).limit(20)
        )
        reminders = result.scalars().all()
    if not reminders:
        await update.message.reply_text("🔔 No active reminders.")
        return
    lines = ["🔔 *Active reminders:*"]
    for reminder in reminders:
        when = reminder.next_trigger.isoformat(timespec="minutes") if reminder.next_trigger else "unscheduled"
        lines.append(f"• {reminder.content} _(next: {when})_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def parse_search_query(args: list[str]) -> str:
    return " ".join(args).strip()


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /search command."""
    query_text = parse_search_query(context.args)
    if not query_text:
        await update.message.reply_text("Usage: /search <query>")
        return

    async with AsyncSessionLocal() as session:
        results = await semantic_search(session, query_text)
    if not results:
        await update.message.reply_text("🔍 No results found.")
        return

    lines = [f"🔍 *Search results for:* _{query_text}_"]
    for item in results[:10]:
        score = item.get("score")
        suffix = f" ({score:.2f})" if isinstance(score, float) else ""
        lines.append(f"• *{item['type'].title()}*{suffix} — {item['title']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /projects command."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task.project).where(Task.project.isnot(None)).distinct())
        projects = [row[0] for row in result.all() if row[0]]
    if not projects:
        await update.message.reply_text("📁 No projects yet.")
        return
    await update.message.reply_text(
        "📁 *Projects:*\n" + "\n".join([f"• {project}" for project in projects]),
        parse_mode="Markdown",
    )


def _callback(action: str, *parts: object) -> str:
    return ":".join([action, *[str(part) for part in parts]])


def build_confirmation_keyboard(
    message_type: MessageType,
    item_id: int | None = None,
    inbox_item_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Build inline keyboard for confirming classification."""
    buttons = []
    entity_id = item_id or 0
    inbox_id = inbox_item_id or 0

    if message_type == MessageType.TASK:
        buttons.append([
            InlineKeyboardButton("✅ Correct", callback_data=_callback("confirm", "task", entity_id, inbox_id)),
            InlineKeyboardButton("🔔 It's a reminder", callback_data=_callback("change", "reminder", "task", entity_id, inbox_id)),
        ])
        buttons.append([
            InlineKeyboardButton("📝 It's a note", callback_data=_callback("change", "note", "task", entity_id, inbox_id)),
            InlineKeyboardButton("✏️ Edit", callback_data=_callback("edit", "task", entity_id, inbox_id)),
        ])
        buttons.append([
            InlineKeyboardButton("➕ Add to My Day", callback_data=f"myday_{entity_id}"),
        ])
    elif message_type == MessageType.REMINDER:
        buttons.append([
            InlineKeyboardButton("✅ Correct", callback_data=_callback("confirm", "reminder", entity_id, inbox_id)),
            InlineKeyboardButton("📋 It's a task", callback_data=_callback("change", "task", "reminder", entity_id, inbox_id)),
        ])
        buttons.append([
            InlineKeyboardButton("📝 It's a note", callback_data=_callback("change", "note", "reminder", entity_id, inbox_id)),
            InlineKeyboardButton("✏️ Edit", callback_data=_callback("edit", "reminder", entity_id, inbox_id)),
        ])
    elif message_type in {MessageType.MEMORY, MessageType.NOTE, MessageType.DISCLOSURE}:
        buttons.append([
            InlineKeyboardButton("✅ Correct", callback_data=_callback("confirm", "memory", entity_id, inbox_id)),
            InlineKeyboardButton("📋 It's a task", callback_data=_callback("change", "task", "memory", entity_id, inbox_id)),
        ])
        buttons.append([
            InlineKeyboardButton("🔔 It's a reminder", callback_data=_callback("change", "reminder", "memory", entity_id, inbox_id)),
            InlineKeyboardButton("✏️ Edit", callback_data=_callback("edit", "memory", entity_id, inbox_id)),
        ])

    return InlineKeyboardMarkup(buttons)


def build_clarification_keyboard(primary_type: str, secondary_type: str, inbox_item_id: int) -> InlineKeyboardMarkup:
    """Quick 2-button keyboard for low-confidence clarifications."""
    type_labels = {
        "task": "📋 Task",
        "reminder": "🔔 Reminder",
        "note": "📝 Note",
        "memory": "🧠 Memory",
    }
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    type_labels.get(primary_type, primary_type),
                    callback_data=f"clarify_{primary_type}_{inbox_item_id}",
                ),
                InlineKeyboardButton(
                    type_labels.get(secondary_type, secondary_type),
                    callback_data=f"clarify_{secondary_type}_{inbox_item_id}",
                ),
            ]
        ]
    )


def build_reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    """Build inline keyboard for reminder nudges."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Done", callback_data=f"reminder_done_{reminder_id}"),
                InlineKeyboardButton("⏰ Snooze 2h", callback_data=f"reminder_snooze_{reminder_id}_120"),
            ],
            [
                InlineKeyboardButton("⏰ Snooze 1h", callback_data=f"reminder_snooze_{reminder_id}_60"),
                InlineKeyboardButton("🌙 Tomorrow", callback_data=f"reminder_tomorrow_{reminder_id}"),
            ],
        ]
    )


async def _save_feedback(
    source_text: str,
    predicted_type: str,
    corrected_type: str,
    confidence: float,
    entity_id: int | None,
    metadata: dict | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            ClassificationFeedback(
                source_text=source_text,
                predicted_type=predicted_type,
                corrected_type=corrected_type,
                confidence=confidence,
                entity_id=entity_id,
                metadata_json=metadata,
            )
        )
        await session.commit()


async def review_recent_feedback_and_improve(limit: int = 200) -> dict:
    """Review corrections and refresh adaptive classifier hints."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ClassificationFeedback)
            .order_by(ClassificationFeedback.created_at.desc())
            .limit(limit)
        )
        feedback_items = result.scalars().all()

    rows = [
        {
            "source_text": item.source_text,
            "predicted_type": item.predicted_type,
            "corrected_type": item.corrected_type,
        }
        for item in feedback_items
    ]
    return get_learning_service().review_and_improve_from_feedback(rows)


async def _get_context_data(item_id: int, message_text: str) -> dict:
    cached = CLASSIFICATION_CONTEXT.get(item_id)
    if cached:
        return cached
    db_ctx = await get_context_for_item(item_id)
    if db_ctx:
        CLASSIFICATION_CONTEXT[item_id] = db_ctx
        return db_ctx
    return {"text": message_text, "predicted_type": "unknown", "confidence": 0.0}


async def _handle_correction(update: Update, result: ClassificationResult, chat_id: int) -> None:
    """Reclassify the most recently captured item using the inbox ledger when possible."""
    last = await get_last_captured_item(chat_id)
    if not last:
        await update.message.reply_text(
            "🤔 I don't have a recent item to correct. What would you like to capture?",
            parse_mode="Markdown",
        )
        return

    new_type_str = (result.extracted_data.get("new_type") or "").lower()
    try:
        new_type = MessageType(new_type_str)
    except ValueError:
        await update.message.reply_text(
            "🤔 Try: 'make that a task', 'make that a reminder', or 'make that a note'.",
            parse_mode="Markdown",
        )
        return

    inbox_item_id = last.get("inbox_item_id")
    original_text = last["text"]
    old_type = last["item_type"] or last["classification_type"] or "unknown"
    item_id = last["item_id"]

    if inbox_item_id and item_id:
        _, new_entity_id, reply = await reclassify_inbox_item(
            inbox_item_id=inbox_item_id,
            source_type=old_type if old_type in {"task", "reminder", "memory"} else "memory",
            source_entity_id=item_id,
            target_type=new_type,
            telegram_message_id=update.message.message_id,
        )
        await _save_feedback(
            source_text=original_text,
            predicted_type=old_type,
            corrected_type=new_type.value,
            confidence=float(result.confidence),
            entity_id=new_entity_id,
            metadata={"action": "correction_intent", "original_item_id": item_id},
        )
        await review_recent_feedback_and_improve()
        keyboard = build_confirmation_keyboard(new_type, new_entity_id, inbox_item_id)
        if new_entity_id is not None:
            CLASSIFICATION_CONTEXT[new_entity_id] = {
                "text": original_text,
                "predicted_type": new_type.value,
                "confidence": 1.0,
            }
        await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=keyboard)
        await save_message(
            chat_id=chat_id,
            role="user",
            text=update.message.text,
            telegram_message_id=update.message.message_id,
            item_id=new_entity_id,
            item_type=new_type.value,
            inbox_item_id=inbox_item_id,
            classification_type=MessageType.CORRECTION.value,
            classification_confidence=result.confidence,
        )
        await save_message(chat_id=chat_id, role="bot", text=reply)
        return

    await update.message.reply_text(
        "🤔 I need a newer capture with inbox tracking to reclassify. "
        "Send the item again, then use the inline buttons or say what to change.",
        parse_mode="Markdown",
    )


async def _handle_update(update: Update, result: ClassificationResult, chat_id: int) -> None:
    """Update a field on the most recently captured task or reminder."""
    last = await get_last_captured_item(chat_id)
    if not last:
        await update.message.reply_text(
            "🤔 I don't have a recent item to update. What would you like to capture?",
            parse_mode="Markdown",
        )
        return

    field = result.extracted_data.get("field", "")
    new_value = result.extracted_data.get("new_value", update.message.text)
    item_id = last["item_id"]
    item_type = last["item_type"]
    reply = None

    if item_type == "task" and item_id:
        async with AsyncSessionLocal() as session:
            r = await session.execute(select(Task).where(Task.id == item_id))
            task = r.scalar_one_or_none()
            if task:
                if field in ("due_date", "time", "date"):
                    parsed = parse_due_date(new_value)
                    task.due_date = parsed
                    reply = (
                        f"📋 Updated *{task.title}*\n📅 Due: "
                        f"{parsed.strftime('%d %b %Y') if parsed else new_value}"
                    )
                elif field == "title":
                    task.title = new_value[:500]
                    reply = f"📋 Task renamed to: *{new_value[:80]}*"
                elif field in ("notes", "project"):
                    setattr(task, field, new_value)
                    reply = f"📋 Updated {field} for *{task.title}*"
                await session.commit()

    elif item_type == "reminder" and item_id:
        async with AsyncSessionLocal() as session:
            r = await session.execute(select(Reminder).where(Reminder.id == item_id))
            reminder = r.scalar_one_or_none()
            if reminder:
                if field in ("time", "due_date", "date", "trigger_time"):
                    parsed = parse_trigger_time(new_value)
                    reminder.trigger_at = parsed
                    reminder.next_trigger = parsed
                    when = parsed.strftime("%d %b %Y %H:%M") if parsed else new_value
                    reply = f"🔔 Updated *{reminder.content[:60]}*\n⏰ Now: {when}"
                elif field == "content":
                    reminder.content = new_value[:200]
                    reply = f"🔔 Reminder updated to: *{new_value[:80]}*"
                await session.commit()

    if not reply:
        reply = "✏️ I couldn't find the item or field to update. Could you be more specific?"

    await update.message.reply_text(reply, parse_mode="Markdown")
    await save_message(
        chat_id=chat_id,
        role="user",
        text=update.message.text,
        telegram_message_id=update.message.message_id,
        classification_type=MessageType.UPDATE.value,
        classification_confidence=result.confidence,
    )
    await save_message(chat_id=chat_id, role="bot", text=reply)


async def _reply_with_outcome(
    message,
    outcome: ProcessingOutcome,
    *,
    chat_id: int,
    user_text: str,
    telegram_message_id: int,
    display_prefix: str | None = None,
) -> None:
    """Send Telegram reply + persist conversation for orchestrator outcomes."""
    body = outcome.reply_text
    if display_prefix:
        body = f"{display_prefix}\n\n{body}"

    if outcome.awaiting_clarification:
        type_map = {
            MessageType.TASK: "task",
            MessageType.REMINDER: "reminder",
            MessageType.NOTE: "note",
            MessageType.MEMORY: "memory",
        }
        primary = type_map.get(outcome.classification.message_type, "task")
        secondary = "reminder" if primary != "reminder" else "task"
        keyboard = build_clarification_keyboard(primary, secondary, outcome.inbox_item_id)
        await message.reply_text(f"🤔 {body}", parse_mode="Markdown", reply_markup=keyboard)
        await save_message(
            chat_id=chat_id,
            role="user",
            text=user_text,
            telegram_message_id=telegram_message_id,
            classification_type=outcome.classification.message_type.value,
            classification_confidence=outcome.classification.confidence,
        )
        await save_message(
            chat_id=chat_id,
            role="bot",
            text=body,
            telegram_message_id=telegram_message_id,
            pending_clarification=True,
            pending_inbox_item_id=outcome.inbox_item_id,
        )
        return

    if outcome.classification.message_type in (MessageType.QUESTION, MessageType.CONVERSATION):
        await message.reply_text(body, parse_mode="Markdown")
        await save_message(
            chat_id=chat_id,
            role="user",
            text=user_text,
            telegram_message_id=telegram_message_id,
            classification_type=outcome.classification.message_type.value,
            classification_confidence=outcome.classification.confidence,
        )
        await save_message(chat_id=chat_id, role="bot", text=outcome.reply_text)
        return

    keyboard = build_confirmation_keyboard(
        outcome.classification.message_type,
        outcome.entity_id,
        outcome.inbox_item_id,
    )
    await message.reply_text(
        body,
        parse_mode="Markdown",
        reply_markup=keyboard if outcome.needs_confirmation and keyboard.inline_keyboard else None,
    )
    if outcome.entity_id is not None:
        CLASSIFICATION_CONTEXT[outcome.entity_id] = {
            "text": user_text,
            "predicted_type": outcome.classification.message_type.value,
            "confidence": outcome.classification.confidence,
        }
    item_type_str = (
        outcome.classification.message_type.value
        if outcome.classification.message_type != MessageType.DISCLOSURE
        else "note"
    )
    await save_message(
        chat_id=chat_id,
        role="user",
        text=user_text,
        telegram_message_id=telegram_message_id,
        item_id=outcome.entity_id,
        item_type=item_type_str if outcome.entity_id else None,
        inbox_item_id=outcome.inbox_item_id,
        classification_type=outcome.classification.message_type.value,
        classification_confidence=outcome.classification.confidence,
    )
    await save_message(chat_id=chat_id, role="bot", text=outcome.reply_text, telegram_message_id=telegram_message_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages."""
    message = update.message
    text = message.text
    chat_id = update.effective_chat.id
    if not text:
        return

    pending_edit = context.user_data.pop("pending_edit", None)
    if pending_edit:
        outcome = await apply_edit(
            inbox_item_id=pending_edit["inbox_item_id"],
            source_type=pending_edit["source_type"],
            source_entity_id=pending_edit.get("source_entity_id"),
            edited_text=text,
            telegram_message_id=message.message_id,
        )
        await _reply_with_outcome(
            message,
            outcome,
            chat_id=chat_id,
            user_text=text,
            telegram_message_id=message.message_id,
            display_prefix="✏️ *Updated capture*",
        )
        return

    pending_clarif = await get_pending_clarification(chat_id)
    if pending_clarif and pending_clarif.pending_inbox_item_id:
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                select(InboxItem).where(InboxItem.id == pending_clarif.pending_inbox_item_id)
            )
            inbox_item = r.scalar_one_or_none()
        if inbox_item:
            await resolve_pending_clarification(pending_clarif.id)
            history = await get_recent_history(chat_id)
            outcome = await resume_inbox_after_clarification(
                inbox_item_id=inbox_item.id,
                additional_user_text=text,
                telegram_message_id=message.message_id,
                conversation_history=history,
            )
            await _reply_with_outcome(
                message,
                outcome,
                chat_id=chat_id,
                user_text=text,
                telegram_message_id=message.message_id,
            )
            return

    logger.info("Received message: %s...", text[:50])
    history = await get_recent_history(chat_id)
    classifier = get_classifier()
    pre = await classifier.classify(text, conversation_history=history)

    if pre.message_type == MessageType.CORRECTION:
        await _handle_correction(update, pre, chat_id)
        return
    if pre.message_type == MessageType.UPDATE:
        await _handle_update(update, pre, chat_id)
        return

    outcome = await process_incoming_content(
        raw_content=text,
        processed_content=text,
        source_type="text",
        telegram_message_id=message.message_id,
        conversation_history=history,
        classification=pre,
    )
    await _reply_with_outcome(
        message,
        outcome,
        chat_id=chat_id,
        user_text=text,
        telegram_message_id=message.message_id,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    logger.info("Callback: %s", data)

    if data.startswith("clarify_"):
        parts = data.split("_", 2)
        if len(parts) < 3:
            await query.edit_message_text("🤔 Invalid clarification button.")
            return
        _, chosen_type_str, inbox_id_str = parts
        try:
            chosen = MessageType(chosen_type_str)
            inbox_id = int(inbox_id_str)
        except ValueError:
            await query.edit_message_text("🤔 Invalid clarification choice.")
            return
        try:
            outcome = await apply_clarification_choice(
                inbox_item_id=inbox_id,
                chosen_type=chosen,
                telegram_message_id=query.message.message_id if query.message else None,
            )
        except Exception:
            logger.exception("Clarification choice failed")
            await query.edit_message_text("🤔 Couldn't apply that choice. Try sending the message again.")
            return
        pending = await get_pending_clarification(chat_id)
        if pending:
            await resolve_pending_clarification(pending.id)
        keyboard = build_confirmation_keyboard(
            outcome.classification.message_type,
            outcome.entity_id,
            outcome.inbox_item_id,
        )
        if outcome.entity_id is not None:
            CLASSIFICATION_CONTEXT[outcome.entity_id] = {
                "text": query.message.text if query.message else "",
                "predicted_type": outcome.classification.message_type.value,
                "confidence": outcome.classification.confidence,
            }
        await query.edit_message_text(
            outcome.reply_text,
            parse_mode="Markdown",
            reply_markup=keyboard if outcome.needs_confirmation and keyboard.inline_keyboard else None,
        )
        return

    if data.startswith("confirm:"):
        _, classification, entity_id_str, inbox_item_id_str = data.split(":")
        entity_id = int(entity_id_str) if entity_id_str.isdigit() else None
        await confirm_classification(
            inbox_item_id=int(inbox_item_id_str),
            classification=classification,
            entity_type=classification,
            entity_id=entity_id,
        )
        ctx = CLASSIFICATION_CONTEXT.get(entity_id) if entity_id is not None else None
        if ctx:
            await _save_feedback(
                source_text=ctx.get("text", query.message.text if query.message else ""),
                predicted_type=ctx.get("predicted_type", "unknown"),
                corrected_type=ctx.get("predicted_type", "unknown"),
                confidence=float(ctx.get("confidence", 0.0)),
                entity_id=entity_id,
                metadata={"action": "confirm"},
            )
        await query.edit_message_text(query.message.text + "\n\n✅ _Confirmed_", parse_mode="Markdown")
        return

    if data.startswith("change:"):
        _, target_type_str, source_type, entity_id_str, inbox_item_id_str = data.split(":")
        target_type = MessageType(target_type_str)
        entity_id = int(entity_id_str) if entity_id_str.isdigit() else None
        ctx = CLASSIFICATION_CONTEXT.get(entity_id) if entity_id is not None else None
        _et, new_entity_id, reply = await reclassify_inbox_item(
            inbox_item_id=int(inbox_item_id_str),
            source_type=source_type,
            source_entity_id=entity_id,
            target_type=target_type,
            telegram_message_id=query.message.message_id if query.message else None,
        )
        await _save_feedback(
            source_text=(ctx or {}).get("text", query.message.text if query.message else ""),
            predicted_type=(ctx or {}).get("predicted_type", "unknown"),
            corrected_type=target_type.value,
            confidence=float((ctx or {}).get("confidence", 0.0)),
            entity_id=entity_id,
            metadata={"action": "change", "target": target_type.value},
        )
        await review_recent_feedback_and_improve()
        new_kb = build_confirmation_keyboard(target_type, new_entity_id, int(inbox_item_id_str))
        if new_entity_id is not None:
            CLASSIFICATION_CONTEXT[new_entity_id] = {
                "text": (ctx or {}).get("text", ""),
                "predicted_type": target_type.value,
                "confidence": 1.0,
            }
        await query.edit_message_text(
            reply,
            parse_mode="Markdown",
            reply_markup=new_kb if new_kb.inline_keyboard else None,
        )
        return

    if data.startswith("edit:"):
        _, source_type, entity_id_str, inbox_item_id_str = data.split(":")
        context.user_data["pending_edit"] = {
            "source_type": source_type,
            "source_entity_id": int(entity_id_str) if entity_id_str.isdigit() else None,
            "inbox_item_id": int(inbox_item_id_str),
        }
        await request_edit(
            inbox_item_id=int(inbox_item_id_str),
            source_type=source_type,
            source_entity_id=int(entity_id_str) if entity_id_str.isdigit() else None,
        )
        await query.edit_message_text(
            "✏️ *Edit requested.* Send me the corrected text and I'll reprocess it.",
            parse_mode="Markdown",
        )
        return

    if data.startswith("reminder_done_"):
        reminder_id = int(data.split("_")[-1])
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
            reminder = result.scalar_one_or_none()
            if reminder:
                if reminder.reminder_type == ReminderType.ONE_OFF:
                    reminder.is_active = False
                else:
                    reminder.current_cycle_done = True
                    reminder.last_acknowledged = datetime.now()
                await session.commit()
        await query.edit_message_text("✅ *Done!* Nice work.", parse_mode="Markdown")
        return

    if data.startswith("reminder_snooze_"):
        parts = data.split("_")
        reminder_id = int(parts[2])
        minutes = int(parts[3])
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
            reminder = result.scalar_one_or_none()
            if reminder:
                reminder.next_trigger = datetime.now() + timedelta(minutes=minutes)
                await session.commit()
        await query.edit_message_text(
            f"⏰ *Snoozed for {minutes} minutes.* I'll check back.",
            parse_mode="Markdown",
        )
        return

    if data.startswith("reminder_tomorrow_"):
        reminder_id = int(data.split("_")[-1])
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
            reminder = result.scalar_one_or_none()
            if reminder:
                tomorrow = datetime.now() + timedelta(days=1)
                reminder.next_trigger = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
                await session.commit()
        await query.edit_message_text("🌙 *Snoozed until tomorrow.* I'll catch you then.", parse_mode="Markdown")
        return

    if data.startswith("myday_"):
        item_id = int(data.split("_")[-1])
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Task).where(Task.id == item_id))
            task = result.scalar_one_or_none()
            if task:
                task.my_day = True
                task.my_day_date = datetime.now()
                await session.commit()
        await query.edit_message_text(query.message.text + "\n\n☀️ _Added to My Day_", parse_mode="Markdown")
        return

    # Legacy callback shapes (older inline keyboards)
    if data.startswith("confirm_"):
        item_id = int(data.split("_")[-1])
        context_data = await _get_context_data(item_id, query.message.text)
        await _save_feedback(
            source_text=context_data.get("text", query.message.text),
            predicted_type=context_data.get("predicted_type", "unknown"),
            corrected_type=context_data.get("predicted_type", "unknown"),
            confidence=float(context_data.get("confidence", 0.0)),
            entity_id=item_id,
            metadata={"action": "confirm"},
        )
        await query.edit_message_text(query.message.text + "\n\n✅ _Confirmed_", parse_mode="Markdown")
        return

    if data.startswith("edit_"):
        await query.edit_message_text(
            "✏️ Please use the *Edit* button on newer messages, or send a fresh capture.",
            parse_mode="Markdown",
        )
        return

    if data.startswith("change_task_"):
        item_id = int(data.split("_")[-1])
        context_data = await _get_context_data(item_id, query.message.text)
        source_text = context_data.get("text", "")
        task_id = await create_task_from_classification(
            title=source_text or "Reclassified task",
            notes="Reclassified from callback",
            telegram_message_id=query.message.message_id,
        )
        await _save_feedback(
            source_text=source_text or query.message.text,
            predicted_type=context_data.get("predicted_type", "unknown"),
            corrected_type=MessageType.TASK.value,
            confidence=float(context_data.get("confidence", 0.0)),
            entity_id=task_id,
            metadata={"action": "change_task", "source_item_id": item_id},
        )
        await review_recent_feedback_and_improve()
        await query.edit_message_text(
            "📋 *Reclassified as task.* Saved and learned from this correction.",
            parse_mode="Markdown",
        )
        return

    if data.startswith("change_reminder_"):
        item_id = int(data.split("_")[-1])
        context_data = await _get_context_data(item_id, query.message.text)
        source_text = context_data.get("text", "")
        reminder_id = await create_reminder_from_classification(
            content=source_text or "Reclassified reminder",
            trigger_time=None,
            is_recurring=False,
            recurrence_pattern=None,
            recurrence_config={"source": "reclassified"},
            telegram_message_id=query.message.message_id,
        )
        await _save_feedback(
            source_text=source_text or query.message.text,
            predicted_type=context_data.get("predicted_type", "unknown"),
            corrected_type=MessageType.REMINDER.value,
            confidence=float(context_data.get("confidence", 0.0)),
            entity_id=reminder_id,
            metadata={"action": "change_reminder", "source_item_id": item_id},
        )
        await review_recent_feedback_and_improve()
        await query.edit_message_text(
            "🔔 *Reclassified as reminder.* Saved and learned from this correction.",
            parse_mode="Markdown",
        )
        return

    if data.startswith("change_note_"):
        item_id = int(data.split("_")[-1])
        context_data = await _get_context_data(item_id, query.message.text)
        await _save_feedback(
            source_text=context_data.get("text", query.message.text),
            predicted_type=context_data.get("predicted_type", "unknown"),
            corrected_type=MessageType.NOTE.value,
            confidence=float(context_data.get("confidence", 0.0)),
            entity_id=item_id,
            metadata={"action": "change_note"},
        )
        await review_recent_feedback_and_improve()
        await query.edit_message_text(
            "📝 *Saved as note.* Learned from this correction.",
            parse_mode="Markdown",
        )
        return


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice notes: transcribe, route, and log the result."""
    message = update.message
    voice = message.voice
    chat_id = update.effective_chat.id
    if not voice:
        return

    try:
        file = await context.bot.get_file(voice.file_id)
        path = os.path.join(tempfile.gettempdir(), f"voice_{voice.file_unique_id}.ogg")
        await file.download_to_drive(custom_path=path)

        settings = get_settings()
        if not settings.openai_api_key:
            await message.reply_text("🎤 Voice received. Transcription requires OPENAI_API_KEY.", parse_mode="Markdown")
            return

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        whisper_model = "whisper-1"
        with open(path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(model=whisper_model, file=audio_file)
        await record_whisper_call(model=whisper_model, response=transcript)
        text = transcript.text.strip() if transcript.text else ""
        try:
            os.remove(path)
        except OSError:
            pass

        if not text:
            await message.reply_text("🎤 Could not transcribe the voice note.", parse_mode="Markdown")
            return

        history = await get_recent_history(chat_id)
        outcome = await process_incoming_content(
            raw_content="[voice note]",
            processed_content=text,
            source_type="voice",
            telegram_message_id=message.message_id,
            telegram_file_id=voice.file_id,
            conversation_history=history,
        )
        prefix = f"🎤 _{text[:100]}..._" if len(text) > 100 else f"🎤 _{text}_"
        await _reply_with_outcome(
            message,
            outcome,
            chat_id=chat_id,
            user_text=text,
            telegram_message_id=message.message_id,
            display_prefix=prefix,
        )
    except Exception as exc:
        logger.exception("Voice handling failed: %s", exc)
        await message.reply_text("🎤 Sorry, I couldn't process that voice note.", parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle images: extract text, route, and log the result."""
    message = update.message
    photo = message.photo[-1]
    chat_id = update.effective_chat.id
    if not photo:
        return

    try:
        settings = get_settings()
        if not settings.openai_api_key:
            await message.reply_text("📸 Image received. Vision requires OPENAI_API_KEY.", parse_mode="Markdown")
            return

        file = await context.bot.get_file(photo.file_id)
        path = os.path.join(tempfile.gettempdir(), f"photo_{photo.file_unique_id}.jpg")
        await file.download_to_drive(custom_path=path)

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        with open(path, "rb") as image_file:
            b64 = base64.b64encode(image_file.read()).decode()
        try:
            os.remove(path)
        except OSError:
            pass

        vision_model = "gpt-4o-mini"
        response = await client.chat.completions.create(
            model=vision_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Extract all text from this image. If it's a screenshot, list the visible text. "
                                "If it's a photo, describe what you see in one sentence. "
                                "Reply with only the extracted text or description, no preamble."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            ],
            max_tokens=500,
        )
        await record_from_openai_chat(model=vision_model, operation="vision", response=response)
        text = (response.choices[0].message.content or "").strip()
        if not text:
            await message.reply_text("📸 Could not extract text from the image.", parse_mode="Markdown")
            return

        history = await get_recent_history(chat_id)
        outcome = await process_incoming_content(
            raw_content="[photo]",
            processed_content=text,
            source_type="image",
            telegram_message_id=message.message_id,
            telegram_file_id=photo.file_id,
            conversation_history=history,
        )
        prefix = f"📸 _{text[:100]}..._" if len(text) > 100 else f"📸 _{text}_"
        await _reply_with_outcome(
            message,
            outcome,
            chat_id=chat_id,
            user_text=text,
            telegram_message_id=message.message_id,
            display_prefix=prefix,
        )
    except Exception as exc:
        logger.exception("Photo handling failed: %s", exc)
        await message.reply_text("📸 Sorry, I couldn't process that image.", parse_mode="Markdown")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads by logging them into the inbox ledger."""
    message = update.message
    document = message.document
    if not document:
        return

    await log_unprocessed_input(
        raw_content=document.file_name or "[document]",
        processed_content=document.file_name or "[document]",
        source_type="document",
        telegram_message_id=message.message_id,
        telegram_file_id=document.file_id,
    )
    await message.reply_text(
        f"📄 *Document received:* {document.file_name}\n"
        "_Saved to inbox for later processing._",
        parse_mode="Markdown",
    )
