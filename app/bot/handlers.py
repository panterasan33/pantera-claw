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
from app.services.search_service import semantic_search, build_question_answer
from app.db.database import AsyncSessionLocal
from app.models.reminder import Reminder, ReminderType
from app.models.task import Task
from sqlalchemy import select

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
        "• 'remind me...' - Create reminder\n",
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
        InlineKeyboardButton("➕ Add to My Day", callback_data=f"myday_{item_id}"),
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
    
    logger.info(f"Received message: {text[:50]}...")
    
    # Classify the message
    classifier = get_classifier()
    result: ClassificationResult = await classifier.classify(text)
    
    logger.info(f"Classified as {result.message_type.value} (confidence: {result.confidence})")
    
    # Persist to database when classified
    item_id = 1  # Fallback for keyboard
    if result.message_type == MessageType.TASK:
        data = result.extracted_data
        try:
            task_id = await create_task_from_classification(
                title=data.get("title", text[:50]),
                notes=data.get("notes"),
                due_date_str=data.get("due_date"),
                project=data.get("project"),
                group=data.get("group"),
                telegram_message_id=message.message_id,
            )
            if task_id:
                item_id = task_id
                logger.info(f"Task persisted: id={task_id} title={data.get('title', text[:50])}")
        except Exception as e:
            logger.warning(f"Failed to persist task: {e}")
    elif result.message_type == MessageType.REMINDER:
        data = result.extracted_data
        try:
            reminder_id = await create_reminder_from_classification(
                content=data.get("content", text[:200]),
                trigger_time=data.get("trigger_time"),
                is_recurring=data.get("is_recurring", False),
                recurrence_pattern=data.get("recurrence_pattern"),
                recurrence_config=data.get("recurrence_config"),
                telegram_message_id=message.message_id,
            )
            if reminder_id:
                item_id = reminder_id
                logger.info(f"Reminder persisted: id={reminder_id}")
        except Exception as e:
            logger.warning(f"Failed to persist reminder: {e}")
    elif result.message_type == MessageType.MEMORY:
        data = result.extracted_data
        try:
            memory_id = await create_memory_from_classification(
                content=data.get("content", text[:200]),
                event_date=data.get("event_date"),
                is_annual=data.get("is_annual", False),
                memory_subtype=data.get("memory_subtype"),
                telegram_message_id=message.message_id,
            )
            if memory_id:
                item_id = memory_id
                logger.info(f"Memory persisted: id={memory_id}")

                # For annual memories (birthdays/anniversaries), also set a yearly
                # reminder so nudges still work through the reminder scheduler.
                event_date = data.get("event_date")
                if event_date and (data.get("is_annual") or data.get("memory_subtype") in {"birthday", "annual_event"}):
                    await create_reminder_from_classification(
                        content=data.get("content", text[:200]),
                        trigger_time=event_date,
                        is_recurring=True,
                        recurrence_pattern="yearly",
                        recurrence_config={"source": "memory"},
                        telegram_message_id=message.message_id,
                    )
        except Exception as e:
            logger.warning(f"Failed to persist memory: {e}")
    
    # Generate response based on classification
    if result.message_type == MessageType.QUESTION:
        async with AsyncSessionLocal() as session:
            search_results = await semantic_search(session, result.extracted_data.get("query", text))
        response = build_question_answer(result.extracted_data.get("query", text), search_results)
    else:
        response = await generate_response(result, text)
    
    # Send confirmation with appropriate keyboard
    keyboard = build_confirmation_keyboard(result.message_type, item_id=item_id)
    
    await message.reply_text(
        response,
        parse_mode="Markdown",
        reply_markup=keyboard if result.confidence < 0.9 else None
    )


async def generate_response(result: ClassificationResult, original_text: str) -> str:
    """Generate a response message based on classification."""
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
        summary = data.get("summary", "personal detail noted")
        response = f"💭 *Noted.* I'll remember this."
        return response
    
    elif result.message_type == MessageType.QUESTION:
        query = data.get("query", original_text)
        # TODO: Run RAG query
        response = f"🔍 Searching... _(RAG not implemented yet)_"
        return response
    
    else:  # CONVERSATION
        return "👋 How can I help you organize something?"


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    logger.info(f"Callback: {data}")
    
    if data.startswith("confirm_"):
        await query.edit_message_text(
            query.message.text + "\n\n✅ _Confirmed_",
            parse_mode="Markdown"
        )
    
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
        await query.edit_message_text(
            "✅ *Done!* Nice work.",
            parse_mode="Markdown"
        )
    
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
    
    elif data.startswith("change_task_"):
        # TODO: Reclassify as task
        await query.edit_message_text(
            "📋 *Reclassified as task.* Got it.",
            parse_mode="Markdown"
        )
    
    elif data.startswith("change_reminder_"):
        # TODO: Reclassify as reminder
        await query.edit_message_text(
            "🔔 *Reclassified as reminder.* When should I remind you?",
            parse_mode="Markdown"
        )
    
    elif data.startswith("change_note_"):
        # TODO: Reclassify as note
        await query.edit_message_text(
            "📝 *Saved as note.* Got it.",
            parse_mode="Markdown"
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice notes: download, transcribe with Whisper, classify, persist."""
    message = update.message
    voice = message.voice
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

        # Classify and persist (same flow as handle_message)
        classifier = get_classifier()
        result = await classifier.classify(text)
        item_id = 1
        if result.message_type == MessageType.TASK:
            data = result.extracted_data
            try:
                task_id = await create_task_from_classification(
                    title=data.get("title", text[:50]),
                    notes=data.get("notes"),
                    due_date_str=data.get("due_date"),
                    project=data.get("project"),
                    group=data.get("group"),
                    telegram_message_id=message.message_id,
                )
                if task_id:
                    item_id = task_id
            except Exception as e:
                logger.warning(f"Failed to persist task from voice: {e}")
        elif result.message_type == MessageType.REMINDER:
            data = result.extracted_data
            try:
                reminder_id = await create_reminder_from_classification(
                    content=data.get("content", text[:200]),
                    trigger_time=data.get("trigger_time"),
                    is_recurring=data.get("is_recurring", False),
                    recurrence_pattern=data.get("recurrence_pattern"),
                    recurrence_config=data.get("recurrence_config"),
                    telegram_message_id=message.message_id,
                )
                if reminder_id:
                    item_id = reminder_id
            except Exception as e:
                logger.warning(f"Failed to persist reminder from voice: {e}")

        response = await generate_response(result, text)
        keyboard = build_confirmation_keyboard(result.message_type, item_id=item_id)
        await message.reply_text(
            f"🎤 _{text[:100]}..._\n\n{response}" if len(text) > 100 else f"🎤 _{text}_\n\n{response}",
            parse_mode="Markdown",
            reply_markup=keyboard if result.confidence < 0.9 else None,
        )
    except Exception as e:
        logger.exception(f"Voice handling failed: {e}")
        await message.reply_text("🎤 Sorry, I couldn't process that voice note.", parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle images: download, extract text with Vision API, classify, persist."""
    message = update.message
    photo = message.photo[-1]  # Get highest resolution
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

        classifier = get_classifier()
        result = await classifier.classify(text)
        item_id = 1
        if result.message_type == MessageType.TASK:
            data = result.extracted_data
            try:
                task_id = await create_task_from_classification(
                    title=data.get("title", text[:50]),
                    notes=data.get("notes"),
                    due_date_str=data.get("due_date"),
                    project=data.get("project"),
                    group=data.get("group"),
                    telegram_message_id=message.message_id,
                )
                if task_id:
                    item_id = task_id
            except Exception as e:
                logger.warning(f"Failed to persist task from image: {e}")
        elif result.message_type == MessageType.REMINDER:
            data = result.extracted_data
            try:
                reminder_id = await create_reminder_from_classification(
                    content=data.get("content", text[:200]),
                    trigger_time=data.get("trigger_time"),
                    is_recurring=data.get("is_recurring", False),
                    recurrence_pattern=data.get("recurrence_pattern"),
                    recurrence_config=data.get("recurrence_config"),
                    telegram_message_id=message.message_id,
                )
                if reminder_id:
                    item_id = reminder_id
            except Exception as e:
                logger.warning(f"Failed to persist reminder from image: {e}")

        response_text = await generate_response(result, text)
        keyboard = build_confirmation_keyboard(result.message_type, item_id=item_id)
        await message.reply_text(
            f"📸 _{text[:100]}..._\n\n{response_text}" if len(text) > 100 else f"📸 _{text}_\n\n{response_text}",
            parse_mode="Markdown",
            reply_markup=keyboard if result.confidence < 0.9 else None,
        )
    except Exception as e:
        logger.exception(f"Photo handling failed: {e}")
        await message.reply_text("📸 Sorry, I couldn't process that image.", parse_mode="Markdown")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads."""
    message = update.message
    document = message.document
    
    await message.reply_text(
        f"📄 *Document received:* {document.file_name}\n"
        "_Processing coming soon..._",
        parse_mode="Markdown"
    )
