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
from app.models.reminder import Reminder, ReminderType
from app.models.task import Task
from app.services.classifier import MessageType
from app.services.llm_usage_service import record_from_openai_chat, record_whisper_call
from app.services.orchestrator import (
    apply_edit,
    confirm_classification,
    log_unprocessed_input,
    process_incoming_content,
    reclassify_inbox_item,
    request_edit,
)
from app.services.search_service import semantic_search

logger = logging.getLogger(__name__)


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
        "• 'remind me...' - Create reminder\n",
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages."""
    message = update.message
    text = message.text
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
        keyboard = build_confirmation_keyboard(
            outcome.classification.message_type,
            outcome.entity_id,
            outcome.inbox_item_id,
        )
        await message.reply_text(
            f"✏️ *Updated capture*\n\n{outcome.reply_text}",
            parse_mode="Markdown",
            reply_markup=keyboard if outcome.needs_confirmation and keyboard.inline_keyboard else None,
        )
        return

    logger.info("Received message: %s...", text[:50])
    outcome = await process_incoming_content(
        raw_content=text,
        processed_content=text,
        source_type="text",
        telegram_message_id=message.message_id,
    )
    keyboard = build_confirmation_keyboard(
        outcome.classification.message_type,
        outcome.entity_id,
        outcome.inbox_item_id,
    )
    await message.reply_text(
        outcome.reply_text,
        parse_mode="Markdown",
        reply_markup=keyboard if outcome.needs_confirmation and keyboard.inline_keyboard else None,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data
    logger.info("Callback: %s", data)

    if data.startswith("confirm:"):
        _, classification, entity_id, inbox_item_id = data.split(":")
        await confirm_classification(
            inbox_item_id=int(inbox_item_id),
            classification=classification,
            entity_type=classification,
            entity_id=int(entity_id) if entity_id.isdigit() else None,
        )
        await query.edit_message_text(query.message.text + "\n\n✅ _Confirmed_", parse_mode="Markdown")
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
        await query.edit_message_text(f"⏰ *Snoozed for {minutes} minutes.* I'll check back.", parse_mode="Markdown")
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

    if data.startswith("change:"):
        _, target_type, source_type, entity_id, inbox_item_id = data.split(":")
        _, _, reply = await reclassify_inbox_item(
            inbox_item_id=int(inbox_item_id),
            source_type=source_type,
            source_entity_id=int(entity_id) if entity_id.isdigit() else None,
            target_type=MessageType(target_type),
            telegram_message_id=query.message.message_id if query.message else None,
        )
        await query.edit_message_text(reply, parse_mode="Markdown")
        return

    if data.startswith("edit:"):
        _, source_type, entity_id, inbox_item_id = data.split(":")
        context.user_data["pending_edit"] = {
            "source_type": source_type,
            "source_entity_id": int(entity_id) if entity_id.isdigit() else None,
            "inbox_item_id": int(inbox_item_id),
        }
        await request_edit(
            inbox_item_id=int(inbox_item_id),
            source_type=source_type,
            source_entity_id=int(entity_id) if entity_id.isdigit() else None,
        )
        await query.edit_message_text(
            "✏️ *Edit requested.* Send me the corrected text and I'll reprocess it.",
            parse_mode="Markdown",
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice notes: transcribe, route, and log the result."""
    message = update.message
    voice = message.voice
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
        with open(path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        text = transcript.text.strip() if transcript.text else ""
        try:
            os.remove(path)
        except OSError:
            pass

        if not text:
            await message.reply_text("🎤 Could not transcribe the voice note.", parse_mode="Markdown")
            return

        outcome = await process_incoming_content(
            raw_content="[voice note]",
            processed_content=text,
            source_type="voice",
            telegram_message_id=message.message_id,
            telegram_file_id=voice.file_id,
        )
        keyboard = build_confirmation_keyboard(
            outcome.classification.message_type,
            outcome.entity_id,
            outcome.inbox_item_id,
        )
        await message.reply_text(
            f"🎤 _{text[:100]}..._\n\n{outcome.reply_text}" if len(text) > 100 else f"🎤 _{text}_\n\n{outcome.reply_text}",
            parse_mode="Markdown",
            reply_markup=keyboard if outcome.needs_confirmation and keyboard.inline_keyboard else None,
        )
    except Exception as exc:
        logger.exception("Voice handling failed: %s", exc)
        await message.reply_text("🎤 Sorry, I couldn't process that voice note.", parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle images: extract text, route, and log the result."""
    message = update.message
    photo = message.photo[-1]
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

        outcome = await process_incoming_content(
            raw_content="[photo]",
            processed_content=text,
            source_type="image",
            telegram_message_id=message.message_id,
            telegram_file_id=photo.file_id,
        )
        keyboard = build_confirmation_keyboard(
            outcome.classification.message_type,
            outcome.entity_id,
            outcome.inbox_item_id,
        )
        await message.reply_text(
            f"📸 _{text[:100]}..._\n\n{outcome.reply_text}" if len(text) > 100 else f"📸 _{text}_\n\n{outcome.reply_text}",
            parse_mode="Markdown",
            reply_markup=keyboard if outcome.needs_confirmation and keyboard.inline_keyboard else None,
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
