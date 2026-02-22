"""
Telegram bot message handlers.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from app.services.classifier import get_classifier, MessageType, ClassificationResult

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
    
    # Generate response based on classification
    response = await generate_response(result, text)
    
    # Send confirmation with appropriate keyboard
    keyboard = build_confirmation_keyboard(result.message_type, item_id=1)  # TODO: actual item ID
    
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
        # TODO: Mark reminder as done
        await query.edit_message_text(
            "✅ *Done!* Nice work.",
            parse_mode="Markdown"
        )
    
    elif data.startswith("reminder_snooze_"):
        parts = data.split("_")
        reminder_id = int(parts[2])
        minutes = int(parts[3])
        # TODO: Snooze reminder
        await query.edit_message_text(
            f"⏰ *Snoozed for {minutes} minutes.* I'll check back.",
            parse_mode="Markdown"
        )
    
    elif data.startswith("myday_"):
        item_id = int(data.split("_")[-1])
        # TODO: Add to My Day
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
    """Handle voice notes."""
    message = update.message
    voice = message.voice
    
    await message.reply_text(
        "🎤 *Voice note received!*\n"
        "_Transcription coming soon..._",
        parse_mode="Markdown"
    )
    # TODO: Download, transcribe with Whisper, then process


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle images and screenshots."""
    message = update.message
    photo = message.photo[-1]  # Get highest resolution
    
    await message.reply_text(
        "📸 *Image received!*\n"
        "_Text extraction coming soon..._",
        parse_mode="Markdown"
    )
    # TODO: Download, process with Vision API, then classify


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads."""
    message = update.message
    document = message.document
    
    await message.reply_text(
        f"📄 *Document received:* {document.file_name}\n"
        "_Processing coming soon..._",
        parse_mode="Markdown"
    )
