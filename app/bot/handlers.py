"""
Telegram bot message handlers.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from datetime import datetime, timedelta

from app.services.classifier import get_classifier, MessageType, ClassificationResult
from app.services.task_service import create_task_from_classification
from app.services.reminder_service import create_reminder_from_classification
from app.services.memory_service import create_memory_from_classification
from app.services.search_service import semantic_search, build_question_answer, build_question_answer_llm
from app.services.classifier_learning import get_learning_service
from app.services.conversation_service import (
    save_message,
    get_recent_history,
    get_last_captured_item,
    get_pending_clarification,
    resolve_pending_clarification,
    get_context_for_item,
)
from app.db.database import AsyncSessionLocal
from app.models.reminder import Reminder, ReminderType
from app.models.task import Task, TaskStatus
from app.models.inbox import InboxItem
from app.models.classification_feedback import ClassificationFeedback
from sqlalchemy import select

logger = logging.getLogger(__name__)


# Runtime context to support callback-driven reclassification.
# Maps callback item IDs to source text + predicted class metadata.
# Falls back to the conversation_messages table on cache miss (e.g. after restart).
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
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(
        "🐆 *Pantera Commands*\n\n"
        "*Capture:*\n"
        "Just send me text, voice, or images - I'll figure out what to do.\n\n"
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
        parse_mode="Markdown"
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
    for t in tasks:
        lines.append(f"• {t.title}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /today command."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.my_day == True).order_by(Task.created_at.desc()).limit(20))
        tasks = result.scalars().all()
    if not tasks:
        await update.message.reply_text("☀️ Nothing in My Day yet.")
        return
    lines = ["☀️ *My Day:*"]
    for t in tasks:
        lines.append(f"• {t.title}")
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
    for r in reminders:
        when = r.next_trigger.isoformat(timespec="minutes") if r.next_trigger else "unscheduled"
        lines.append(f"• {r.content} _(next: {when})_")
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
        lines.append(f"• *{item['type'].title()}* — {item['title']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /projects command."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task.project).where(Task.project.isnot(None)).distinct())
        projects = [row[0] for row in result.all() if row[0]]
    if not projects:
        await update.message.reply_text("📁 No projects yet.")
        return
    await update.message.reply_text("📁 *Projects:*\n" + "\n".join([f"• {p}" for p in projects]), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------

def build_confirmation_keyboard(message_type: MessageType, item_id: int = None) -> InlineKeyboardMarkup:
    """Build inline keyboard for confirming classification."""
    buttons = []

    if message_type == MessageType.TASK:
        buttons.append([
            InlineKeyboardButton("✅ Correct", callback_data=f"confirm_task_{item_id}"),
            InlineKeyboardButton("🔔 It's a reminder", callback_data=f"change_reminder_{item_id}"),
        ])
        buttons.append([
            InlineKeyboardButton("📝 It's a note", callback_data=f"change_note_{item_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"edit_{item_id}"),
        ])
    elif message_type == MessageType.REMINDER:
        buttons.append([
            InlineKeyboardButton("✅ Correct", callback_data=f"confirm_reminder_{item_id}"),
            InlineKeyboardButton("📋 It's a task", callback_data=f"change_task_{item_id}"),
        ])
        buttons.append([
            InlineKeyboardButton("📝 It's a note", callback_data=f"change_note_{item_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"edit_{item_id}"),
        ])
    elif message_type == MessageType.NOTE:
        buttons.append([
            InlineKeyboardButton("✅ Correct", callback_data=f"confirm_note_{item_id}"),
            InlineKeyboardButton("📋 It's a task", callback_data=f"change_task_{item_id}"),
        ])
        buttons.append([
            InlineKeyboardButton("🔔 It's a reminder", callback_data=f"change_reminder_{item_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"edit_{item_id}"),
        ])
    elif message_type == MessageType.MEMORY:
        buttons.append([
            InlineKeyboardButton("✅ Correct", callback_data=f"confirm_memory_{item_id}"),
            InlineKeyboardButton("📋 It's a task", callback_data=f"change_task_{item_id}"),
        ])

    buttons.append([
        InlineKeyboardButton("➕ Add to My Day", callback_data=f"myday_{item_id}"),
    ])

    return InlineKeyboardMarkup(buttons)


def build_clarification_keyboard(primary_type: str, secondary_type: str, item_id: int) -> InlineKeyboardMarkup:
    """Quick 2-button keyboard for clarifying questions."""
    type_labels = {
        "task": "📋 Task",
        "reminder": "🔔 Reminder",
        "note": "📝 Note",
        "memory": "🧠 Memory",
    }
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(type_labels.get(primary_type, primary_type), callback_data=f"clarify_{primary_type}_{item_id}"),
        InlineKeyboardButton(type_labels.get(secondary_type, secondary_type), callback_data=f"clarify_{secondary_type}_{item_id}"),
    ]])


def build_reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    """Build inline keyboard for reminder nudges."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Done", callback_data=f"reminder_done_{reminder_id}"),
            InlineKeyboardButton("⏰ Snooze 2h", callback_data=f"reminder_snooze_{reminder_id}_120"),
        ],
        [
            InlineKeyboardButton("⏰ Snooze 1h", callback_data=f"reminder_snooze_{reminder_id}_60"),
            InlineKeyboardButton("🌙 Tomorrow", callback_data=f"reminder_tomorrow_{reminder_id}"),
        ],
    ])


# ---------------------------------------------------------------------------
# Core persistence helper (shared by text / voice / photo handlers)
# ---------------------------------------------------------------------------

async def _persist_classified_item(
    result: ClassificationResult,
    text: str,
    telegram_message_id: int,
) -> int:
    """
    Persist the classified item to the appropriate table.
    Returns the DB id of the created item (falls back to telegram_message_id).
    """
    item_id = telegram_message_id
    data = result.extracted_data

    if result.message_type == MessageType.TASK:
        try:
            task_id = await create_task_from_classification(
                title=data.get("title", text[:50]),
                notes=data.get("notes"),
                due_date_str=data.get("due_date"),
                project=data.get("project"),
                group=data.get("group"),
                telegram_message_id=telegram_message_id,
            )
            if task_id:
                item_id = task_id
        except Exception as e:
            logger.warning(f"Failed to persist task: {e}")

    elif result.message_type == MessageType.REMINDER:
        try:
            reminder_id = await create_reminder_from_classification(
                content=data.get("content", text[:200]),
                trigger_time=data.get("trigger_time"),
                is_recurring=data.get("is_recurring", False),
                recurrence_pattern=data.get("recurrence_pattern"),
                recurrence_config=data.get("recurrence_config"),
                telegram_message_id=telegram_message_id,
            )
            if reminder_id:
                item_id = reminder_id
        except Exception as e:
            logger.warning(f"Failed to persist reminder: {e}")

    elif result.message_type == MessageType.MEMORY:
        try:
            memory_id = await create_memory_from_classification(
                content=data.get("content", text[:200]),
                event_date=data.get("event_date"),
                is_annual=data.get("is_annual", False),
                memory_subtype=data.get("memory_subtype"),
                telegram_message_id=telegram_message_id,
            )
            if memory_id:
                item_id = memory_id
                # Also create a yearly reminder for annual events
                event_date = data.get("event_date")
                if event_date and (data.get("is_annual") or data.get("memory_subtype") in {"birthday", "annual_event"}):
                    await create_reminder_from_classification(
                        content=data.get("content", text[:200]),
                        trigger_time=event_date,
                        is_recurring=True,
                        recurrence_pattern="yearly",
                        recurrence_config={"source": "memory"},
                        telegram_message_id=telegram_message_id,
                    )
        except Exception as e:
            logger.warning(f"Failed to persist memory: {e}")

    elif result.message_type in (MessageType.NOTE, MessageType.DISCLOSURE):
        try:
            async with AsyncSessionLocal() as session:
                inbox = InboxItem(
                    raw_content=text,
                    classification=result.message_type.value,
                    classification_confidence=result.confidence,
                    extracted_data=data,
                    is_processed=True,
                    source_type="text",
                    telegram_message_id=telegram_message_id,
                )
                session.add(inbox)
                await session.flush()
                item_id = inbox.id
                await session.commit()
        except Exception as e:
            logger.warning(f"Failed to persist note/disclosure: {e}")

    return item_id


# ---------------------------------------------------------------------------
# Clarifying question generation
# ---------------------------------------------------------------------------

async def _generate_clarifying_question(text: str, result: ClassificationResult) -> str:
    """Ask the LLM to produce a single focused clarifying question."""
    from app.config import get_settings
    settings = get_settings()

    prompt = (
        f"A personal assistant received this message: \"{text}\"\n"
        f"It was classified as '{result.message_type.value}' with {result.confidence:.0%} confidence.\n"
        "Generate ONE short clarifying question (max 15 words) to confirm the user's intent. "
        "For reminders ask about timing; for tasks ask about deadline or priority; "
        "for notes ask if action is needed. Reply with only the question."
    )

    try:
        if settings.anthropic_api_key:
            import anthropic as _anthropic
            client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            resp = await client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        elif settings.openai_api_key:
            import openai as _openai
            client = _openai.AsyncOpenAI(api_key=settings.openai_api_key)
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning(f"Failed to generate clarifying question: {e}")

    # Static fallback
    if result.message_type == MessageType.REMINDER:
        return "When should I remind you about this?"
    if result.message_type == MessageType.TASK:
        return "Is this something you need to do, or just a note to remember?"
    return "Did you want me to save this as a task or a note?"


# ---------------------------------------------------------------------------
# CORRECTION intent handler
# ---------------------------------------------------------------------------

async def _handle_correction(
    update: Update,
    result: ClassificationResult,
    chat_id: int,
) -> None:
    """Handle CORRECTION intent: reclassify the most recently captured item."""
    last = await get_last_captured_item(chat_id)
    if not last:
        await update.message.reply_text(
            "🤔 I don't have a recent item to correct. What would you like to capture?",
            parse_mode="Markdown",
        )
        return

    new_type_str = result.extracted_data.get("new_type") or ""
    try:
        new_type = MessageType(new_type_str.lower())
    except ValueError:
        await update.message.reply_text(
            "🤔 Not sure what type you'd like. Try: 'make that a task', 'make that a reminder', or 'make that a note'.",
            parse_mode="Markdown",
        )
        return

    original_text = last["text"]
    old_type = last["item_type"] or last["classification_type"] or "unknown"
    item_id = last["item_id"]

    # Re-classify with the forced type to get proper extracted_data
    classifier = get_classifier()
    forced_result = await classifier.classify(f"{new_type_str}: {original_text}")
    forced_result.message_type = new_type

    new_item_id = await _persist_classified_item(
        forced_result, original_text, update.message.message_id
    )

    await _save_feedback(
        source_text=original_text,
        predicted_type=old_type,
        corrected_type=new_type.value,
        confidence=0.0,
        entity_id=new_item_id,
        metadata={"action": "correction_intent", "original_item_id": item_id},
    )
    await review_recent_feedback_and_improve()

    type_icons = {"task": "📋", "reminder": "🔔", "note": "📝", "memory": "🧠"}
    icon = type_icons.get(new_type.value, "✅")
    reply = (
        f"{icon} *Got it!* Changed from {old_type} → *{new_type.value}*\n"
        f"_{original_text[:80]}_"
    )
    keyboard = build_confirmation_keyboard(new_type, item_id=new_item_id)
    await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=keyboard)

    CLASSIFICATION_CONTEXT[new_item_id] = {
        "text": original_text,
        "predicted_type": new_type.value,
        "confidence": 1.0,
    }
    await save_message(
        chat_id=chat_id, role="user", text=update.message.text,
        telegram_message_id=update.message.message_id,
        item_id=new_item_id, item_type=new_type.value,
        classification_type=MessageType.CORRECTION.value,
        classification_confidence=result.confidence,
    )
    await save_message(chat_id=chat_id, role="bot", text=reply)


# ---------------------------------------------------------------------------
# UPDATE intent handler
# ---------------------------------------------------------------------------

async def _handle_update(
    update: Update,
    result: ClassificationResult,
    chat_id: int,
) -> None:
    """Handle UPDATE intent: modify a field of the most recently captured item."""
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
        from app.services.task_service import parse_due_date
        async with AsyncSessionLocal() as session:
            r = await session.execute(select(Task).where(Task.id == item_id))
            task = r.scalar_one_or_none()
            if task:
                if field in ("due_date", "time", "date"):
                    parsed = parse_due_date(new_value)
                    task.due_date = parsed
                    reply = f"📋 Updated *{task.title}*\n📅 Due: {parsed.strftime('%d %b %Y') if parsed else new_value}"
                elif field == "title":
                    task.title = new_value[:500]
                    reply = f"📋 Task renamed to: *{new_value[:80]}*"
                elif field in ("notes", "project"):
                    setattr(task, field, new_value)
                    reply = f"📋 Updated {field} for *{task.title}*"
                await session.commit()

    elif item_type == "reminder" and item_id:
        from app.services.reminder_service import parse_trigger_time
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
        chat_id=chat_id, role="user", text=update.message.text,
        telegram_message_id=update.message.message_id,
        classification_type=MessageType.UPDATE.value,
        classification_confidence=result.confidence,
    )
    await save_message(chat_id=chat_id, role="bot", text=reply)


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages."""
    message = update.message
    text = message.text
    chat_id = update.effective_chat.id

    if not text:
        return

    logger.info(f"Received message: {text[:50]}...")

    # ── Check if bot is awaiting clarification ──────────────────────────────
    pending_clarif = await get_pending_clarification(chat_id)
    if pending_clarif and pending_clarif.pending_inbox_item_id:
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                select(InboxItem).where(InboxItem.id == pending_clarif.pending_inbox_item_id)
            )
            inbox_item = r.scalar_one_or_none()

        if inbox_item:
            await resolve_pending_clarification(pending_clarif.id)
            combined = f"{inbox_item.raw_content}. (additional context: {text})"
            history = await get_recent_history(chat_id)
            classifier = get_classifier()
            result = await classifier.classify(combined, conversation_history=history)
            return await _finish_message_handling(update, result, inbox_item.raw_content, chat_id)

    # ── Fetch conversation history & classify ────────────────────────────────
    history = await get_recent_history(chat_id)
    classifier = get_classifier()
    result: ClassificationResult = await classifier.classify(text, conversation_history=history)
    logger.info(f"Classified as {result.message_type.value} (confidence: {result.confidence})")

    # ── Route correction / update intents ────────────────────────────────────
    if result.message_type == MessageType.CORRECTION:
        await _handle_correction(update, result, chat_id)
        return

    if result.message_type == MessageType.UPDATE:
        await _handle_update(update, result, chat_id)
        return

    await _finish_message_handling(update, result, text, chat_id)


async def _finish_message_handling(
    update: Update,
    result: ClassificationResult,
    text: str,
    chat_id: int,
):
    """Persist item (or ask clarification) and send bot response."""
    message = update.message

    # ── Low-confidence clarification gate ────────────────────────────────────
    CLARIFICATION_THRESHOLD = 0.65
    if (
        result.confidence < CLARIFICATION_THRESHOLD
        and result.message_type not in (MessageType.QUESTION, MessageType.CONVERSATION,
                                        MessageType.CORRECTION, MessageType.UPDATE)
    ):
        inbox_id = None
        try:
            async with AsyncSessionLocal() as session:
                inbox = InboxItem(
                    raw_content=text,
                    classification=result.message_type.value,
                    classification_confidence=result.confidence,
                    extracted_data=result.extracted_data,
                    is_processed=False,
                    needs_clarification=True,
                    source_type="text",
                    telegram_message_id=message.message_id,
                )
                session.add(inbox)
                await session.flush()
                inbox_id = inbox.id
                await session.commit()
        except Exception as e:
            logger.warning(f"Failed to save inbox item for clarification: {e}")

        question = await _generate_clarifying_question(text, result)

        type_map = {
            MessageType.TASK: "task", MessageType.REMINDER: "reminder",
            MessageType.NOTE: "note", MessageType.MEMORY: "memory",
        }
        primary = type_map.get(result.message_type, "task")
        secondary = "reminder" if primary != "reminder" else "task"
        keyboard = build_clarification_keyboard(primary, secondary, inbox_id or message.message_id)

        await message.reply_text(f"🤔 {question}", parse_mode="Markdown", reply_markup=keyboard)
        await save_message(
            chat_id=chat_id, role="user", text=text,
            telegram_message_id=message.message_id,
            classification_type=result.message_type.value,
            classification_confidence=result.confidence,
        )
        await save_message(
            chat_id=chat_id, role="bot", text=question,
            telegram_message_id=message.message_id,
            pending_clarification=True,
            pending_inbox_item_id=inbox_id,
        )
        return

    # ── QUESTION: semantic search + LLM answer ───────────────────────────────
    if result.message_type == MessageType.QUESTION:
        async with AsyncSessionLocal() as session:
            search_results = await semantic_search(session, result.extracted_data.get("query", text))
        response = await build_question_answer_llm(
            result.extracted_data.get("query", text), search_results
        )
        await message.reply_text(response, parse_mode="Markdown")
        await save_message(
            chat_id=chat_id, role="user", text=text,
            telegram_message_id=message.message_id,
            classification_type=MessageType.QUESTION.value,
            classification_confidence=result.confidence,
        )
        await save_message(chat_id=chat_id, role="bot", text=response)
        return

    # ── CONVERSATION: LLM-powered smart reply ────────────────────────────────
    if result.message_type == MessageType.CONVERSATION:
        history = await get_recent_history(chat_id)
        response = await _handle_conversation(text, history=history)
        await message.reply_text(response, parse_mode="Markdown")
        await save_message(
            chat_id=chat_id, role="user", text=text,
            telegram_message_id=message.message_id,
            classification_type=MessageType.CONVERSATION.value,
            classification_confidence=result.confidence,
        )
        await save_message(chat_id=chat_id, role="bot", text=response)
        return

    # ── Persist item ─────────────────────────────────────────────────────────
    item_id = await _persist_classified_item(result, text, message.message_id)
    logger.info(f"Persisted {result.message_type.value}: id={item_id}")

    CLASSIFICATION_CONTEXT[item_id] = {
        "text": text,
        "predicted_type": result.message_type.value,
        "confidence": result.confidence,
    }

    response = _build_response(result, text)
    keyboard = build_confirmation_keyboard(result.message_type, item_id=item_id)
    await message.reply_text(response, parse_mode="Markdown", reply_markup=keyboard)

    item_type_str = result.message_type.value if result.message_type != MessageType.DISCLOSURE else "note"
    await save_message(
        chat_id=chat_id, role="user", text=text,
        telegram_message_id=message.message_id,
        item_id=item_id, item_type=item_type_str,
        classification_type=result.message_type.value,
        classification_confidence=result.confidence,
    )
    await save_message(chat_id=chat_id, role="bot", text=response,
                       telegram_message_id=message.message_id)


async def _handle_conversation(text: str, history: list[dict]) -> str:
    """Generate a context-aware reply for CONVERSATION messages."""
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
            resp = await client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        elif settings.openai_api_key:
            import openai as _openai
            client = _openai.AsyncOpenAI(api_key=settings.openai_api_key)
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning(f"CONVERSATION LLM failed: {e}")

    return "👋 I'm here! Send me tasks, reminders, or notes and I'll keep everything organized."


# ---------------------------------------------------------------------------
# Response text builder
# ---------------------------------------------------------------------------

def _build_response(result: ClassificationResult, original_text: str) -> str:
    """Build a confirmation response string from a classification result."""
    data = result.extracted_data

    if result.message_type == MessageType.TASK:
        title = data.get("title", original_text[:50])
        due = data.get("due_date")
        project = data.get("project")
        response = f"📋 *Task captured:* {title}"
        if due:
            response += f"\n📅 Due: {due}"
        if project:
            response += f"\n📁 Project: {project}"
        return response

    elif result.message_type == MessageType.REMINDER:
        content = data.get("content", original_text[:50])
        trigger = data.get("trigger_time", "not set")
        recurring = data.get("is_recurring", False)
        response = f"🔔 *Reminder set:* {content}"
        response += f"\n⏰ {trigger}"
        if recurring:
            pattern = data.get("recurrence_detail", data.get("recurrence_pattern", ""))
            response += f"\n🔁 Recurring: {pattern}"
        return response

    elif result.message_type == MessageType.MEMORY:
        content = data.get("content", original_text[:50])
        subtype = data.get("memory_subtype", "event")
        response = f"🧠 *Remembered:* {content}"
        if subtype == "birthday":
            response += "\n🎂 I'll remind you before the day"
        else:
            response += "\n📆 I'll surface this at the right time"
        return response

    elif result.message_type == MessageType.NOTE:
        content = data.get("content", original_text[:100])
        tags = data.get("tags", [])
        response = f"📝 *Note saved:* {content[:100]}"
        if tags:
            response += f"\n🏷️ Tags: {', '.join(tags)}"
        return response

    elif result.message_type == MessageType.DISCLOSURE:
        return "💭 *Noted.* I'll remember this."

    return "👋 Captured! Use the buttons to correct if needed."


# ---------------------------------------------------------------------------
# Feedback helpers
# ---------------------------------------------------------------------------

async def _save_feedback(
    source_text: str,
    predicted_type: str,
    corrected_type: str,
    confidence: float,
    entity_id: int | None,
    metadata: dict | None = None,
):
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
    """Self-improving step: review corrections and update adaptive classifier hints."""
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


# ---------------------------------------------------------------------------
# Callback handler
# ---------------------------------------------------------------------------

async def _get_context_data(item_id: int, message_text: str) -> dict:
    """Return classification context, falling back to DB on cache miss."""
    cached = CLASSIFICATION_CONTEXT.get(item_id)
    if cached:
        return cached
    db_ctx = await get_context_for_item(item_id)
    if db_ctx:
        CLASSIFICATION_CONTEXT[item_id] = db_ctx  # warm the cache
        return db_ctx
    return {"text": message_text, "predicted_type": "unknown", "confidence": 0.0}


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data
    logger.info(f"Callback: {data}")

    # ── Clarification quick-reply ─────────────────────────────────────────────
    if data.startswith("clarify_"):
        parts = data.split("_")
        chosen_type = parts[1]
        inbox_id = int(parts[2])
        chat_id = update.effective_chat.id

        async with AsyncSessionLocal() as session:
            r = await session.execute(select(InboxItem).where(InboxItem.id == inbox_id))
            inbox = r.scalar_one_or_none()
        if not inbox:
            await query.edit_message_text("🤔 Couldn't find the original message.")
            return
        original_text = inbox.raw_content

        classifier = get_classifier()
        forced_result = await classifier.classify(f"{chosen_type}: {original_text}")
        try:
            forced_result.message_type = MessageType(chosen_type)
        except ValueError:
            pass

        item_id = await _persist_classified_item(forced_result, original_text, query.message.message_id)
        CLASSIFICATION_CONTEXT[item_id] = {
            "text": original_text,
            "predicted_type": chosen_type,
            "confidence": 1.0,
        }
        pending = await get_pending_clarification(chat_id)
        if pending:
            await resolve_pending_clarification(pending.id)

        response = _build_response(forced_result, original_text)
        try:
            kb_type = MessageType(chosen_type)
        except ValueError:
            kb_type = MessageType.NOTE
        keyboard = build_confirmation_keyboard(kb_type, item_id=item_id)
        await query.edit_message_text(response, parse_mode="Markdown", reply_markup=keyboard)
        await save_message(
            chat_id=chat_id,
            role="user",
            text=f"[clarified as {chosen_type}] {original_text}",
            telegram_message_id=query.message.message_id,
            item_id=item_id, item_type=chosen_type,
            classification_type=chosen_type, classification_confidence=1.0,
        )
        return

    # ── Confirm ───────────────────────────────────────────────────────────────
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
        await query.edit_message_text(
            query.message.text + "\n\n✅ _Confirmed_",
            parse_mode="Markdown"
        )

    # ── Edit ─────────────────────────────────────────────────────────────────
    elif data.startswith("edit_"):
        item_id = int(data.split("_")[-1])
        chat_id = update.effective_chat.id
        # Mark a pending clarification so next user message replaces this item's content
        await save_message(
            chat_id=chat_id, role="bot",
            text="[EDIT_PENDING]",
            pending_clarification=True,
            pending_inbox_item_id=item_id,
        )
        await query.edit_message_text(
            query.message.text + "\n\n✏️ _Send the updated text and I'll replace this item._",
            parse_mode="Markdown",
        )

    # ── Reminder done ─────────────────────────────────────────────────────────
    elif data.startswith("reminder_done_"):
        reminder_id = int(data.split("_")[-1])
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
            r = result.scalar_one_or_none()
            if r:
                if r.reminder_type == ReminderType.ONE_OFF:
                    r.is_active = False
                else:
                    r.current_cycle_done = True
                    r.last_acknowledged = datetime.now()
                await session.commit()
        await query.edit_message_text("✅ *Done!* Nice work.", parse_mode="Markdown")

    # ── Reminder snooze ───────────────────────────────────────────────────────
    elif data.startswith("reminder_snooze_"):
        parts = data.split("_")
        reminder_id = int(parts[2])
        minutes = int(parts[3])
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
            r = result.scalar_one_or_none()
            if r:
                r.next_trigger = datetime.now() + timedelta(minutes=minutes)
                await session.commit()
        await query.edit_message_text(
            f"⏰ *Snoozed for {minutes} minutes.* I'll check back.",
            parse_mode="Markdown"
        )

    # ── Reminder tomorrow ─────────────────────────────────────────────────────
    elif data.startswith("reminder_tomorrow_"):
        reminder_id = int(data.split("_")[-1])
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
            r = result.scalar_one_or_none()
            if r:
                tomorrow = datetime.now() + timedelta(days=1)
                r.next_trigger = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
                await session.commit()
        await query.edit_message_text(
            "🌙 *Snoozed until tomorrow.* I'll catch you then.",
            parse_mode="Markdown"
        )

    # ── Add to My Day ─────────────────────────────────────────────────────────
    elif data.startswith("myday_"):
        item_id = int(data.split("_")[-1])
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Task).where(Task.id == item_id))
            t = result.scalar_one_or_none()
            if t:
                t.my_day = True
                t.my_day_date = datetime.now()
                await session.commit()
        await query.edit_message_text(
            query.message.text + "\n\n☀️ _Added to My Day_",
            parse_mode="Markdown"
        )

    # ── Change to task ────────────────────────────────────────────────────────
    elif data.startswith("change_task_"):
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
            parse_mode="Markdown"
        )

    # ── Change to reminder ────────────────────────────────────────────────────
    elif data.startswith("change_reminder_"):
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
            parse_mode="Markdown"
        )

    # ── Change to note ────────────────────────────────────────────────────────
    elif data.startswith("change_note_"):
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
            parse_mode="Markdown"
        )


# ---------------------------------------------------------------------------
# Voice handler
# ---------------------------------------------------------------------------

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice notes: download, transcribe with Whisper, classify, persist."""
    message = update.message
    voice = message.voice
    chat_id = update.effective_chat.id
    if not voice:
        return

    try:
        import tempfile
        import os
        file = await context.bot.get_file(voice.file_id)
        path = os.path.join(tempfile.gettempdir(), f"voice_{voice.file_unique_id}.ogg")
        await file.download_to_drive(custom_path=path)

        from openai import AsyncOpenAI
        from app.config import get_settings
        settings = get_settings()
        if not settings.openai_api_key:
            await message.reply_text("🎤 Voice received. Transcription requires OPENAI_API_KEY.", parse_mode="Markdown")
            return

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        with open(path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
        text = transcript.text.strip() if transcript.text else ""
        try:
            os.remove(path)
        except OSError:
            pass

        if not text:
            await message.reply_text("🎤 Could not transcribe the voice note.", parse_mode="Markdown")
            return

        history = await get_recent_history(chat_id)
        classifier = get_classifier()
        result = await classifier.classify(text, conversation_history=history)

        item_id = await _persist_classified_item(result, text, message.message_id)

        CLASSIFICATION_CONTEXT[item_id] = {
            "text": text,
            "predicted_type": result.message_type.value,
            "confidence": result.confidence,
        }

        response = _build_response(result, text)
        keyboard = build_confirmation_keyboard(result.message_type, item_id=item_id)
        display = f"🎤 _{text[:100]}..._\n\n{response}" if len(text) > 100 else f"🎤 _{text}_\n\n{response}"
        await message.reply_text(display, parse_mode="Markdown", reply_markup=keyboard)

        await save_message(
            chat_id=chat_id, role="user", text=text,
            telegram_message_id=message.message_id,
            item_id=item_id, item_type=result.message_type.value,
            classification_type=result.message_type.value,
            classification_confidence=result.confidence,
        )
        await save_message(chat_id=chat_id, role="bot", text=response)

    except Exception as e:
        logger.exception(f"Voice handling failed: {e}")
        await message.reply_text("🎤 Sorry, I couldn't process that voice note.", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Photo handler
# ---------------------------------------------------------------------------

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle images: download, extract text with Vision API, classify, persist."""
    message = update.message
    photo = message.photo[-1]
    chat_id = update.effective_chat.id
    if not photo:
        return

    try:
        import tempfile
        import os
        import base64
        from openai import AsyncOpenAI
        from app.config import get_settings

        settings = get_settings()
        if not settings.openai_api_key:
            await message.reply_text("📸 Image received. Vision requires OPENAI_API_KEY.", parse_mode="Markdown")
            return

        file = await context.bot.get_file(photo.file_id)
        path = os.path.join(tempfile.gettempdir(), f"photo_{photo.file_unique_id}.jpg")
        await file.download_to_drive(custom_path=path)

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        with open(path, "rb") as img_file:
            b64 = base64.b64encode(img_file.read()).decode()
        try:
            os.remove(path)
        except OSError:
            pass

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract all text from this image. If it's a screenshot, list the visible text. If it's a photo, describe what you see in one sentence. Reply with only the extracted text or description, no preamble."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            ],
            max_tokens=500,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            await message.reply_text("📸 Could not extract text from the image.", parse_mode="Markdown")
            return

        history = await get_recent_history(chat_id)
        classifier = get_classifier()
        result = await classifier.classify(text, conversation_history=history)

        item_id = await _persist_classified_item(result, text, message.message_id)

        CLASSIFICATION_CONTEXT[item_id] = {
            "text": text,
            "predicted_type": result.message_type.value,
            "confidence": result.confidence,
        }

        response_text = _build_response(result, text)
        keyboard = build_confirmation_keyboard(result.message_type, item_id=item_id)
        display = f"📸 _{text[:100]}..._\n\n{response_text}" if len(text) > 100 else f"📸 _{text}_\n\n{response_text}"
        await message.reply_text(display, parse_mode="Markdown", reply_markup=keyboard)

        await save_message(
            chat_id=chat_id, role="user", text=text,
            telegram_message_id=message.message_id,
            item_id=item_id, item_type=result.message_type.value,
            classification_type=result.message_type.value,
            classification_confidence=result.confidence,
        )
        await save_message(chat_id=chat_id, role="bot", text=response_text)

    except Exception as e:
        logger.exception(f"Photo handling failed: {e}")
        await message.reply_text("📸 Sorry, I couldn't process that image.", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Document handler
# ---------------------------------------------------------------------------

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads."""
    message = update.message
    document = message.document

    await message.reply_text(
        f"📄 *Document received:* {document.file_name}\n"
        "_Processing coming soon..._",
        parse_mode="Markdown"
    )
