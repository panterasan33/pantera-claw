# 🐆 Pantera

Personal assistant that captures, classifies, and organizes everything you throw at it.

## Features

- **Smart Classification**: Text, voice, images → automatically sorted into tasks, reminders, notes, or memory items
- **Persistent Reminders**: Keeps nudging until you acknowledge
- **Long-term Memory**: Birthdays, MOT, renewals — surfaces them at the right time
- **RAG-powered Search**: Ask "what did I say about X?" and get answers
- **My Day**: Daily focus view with morning briefings

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .config/secrets.env.example .config/secrets.env
# Edit secrets.env with your tokens

# Run (needs Postgres with pgvector)
python main.py
```

## Switching between local and Railway

Only one bot instance can poll Telegram at a time. Use the switch script:

```bash
# Interactive menu
python scripts/switch_mode.py

# Or directly
python scripts/switch_mode.py local   # Run locally
python scripts/switch_mode.py railway # Deploy to Railway
python scripts/switch_mode.py off    # Stop Railway only (run before local if you see conflicts)

# Or via Make
make local    # Run locally
make railway  # Deploy to Railway
make off      # Stop Railway deployment
```

**For automatic Railway stop**: Run `railway login` and `railway link` once. Then the switch script can stop the Railway deployment when switching to local.

**In VS Code**: Run Task > "Pantera: Run Local", "Pantera: Deploy to Railway", or "Pantera: Stop Railway"

## Environment Variables

```
TELEGRAM_BOT_TOKEN=your_bot_token
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/pantera
OPENAI_API_KEY=your_openai_key  # For Whisper, embeddings
ANTHROPIC_API_KEY=your_anthropic_key  # For classification (optional)
```

## Architecture

```
pantera/
├── app/
│   ├── bot/          # Telegram bot handlers
│   ├── api/          # FastAPI endpoints (web app)
│   ├── db/           # Database connection
│   ├── models/       # SQLAlchemy models
│   ├── services/     # Business logic (classifier, RAG, etc.)
│   └── config.py     # Settings
├── main.py           # Entry point
└── requirements.txt
```

## Status

🚧 **In Development**

- [x] Project structure
- [x] Database models (Task, Reminder, Memory, Inbox)
- [x] Classification engine (LLM + rule-based fallback)
- [x] Basic Telegram bot skeleton
- [ ] Task CRUD
- [ ] Reminder engine with nudging
- [ ] Voice transcription (Whisper)
- [ ] Image processing (Vision)
- [ ] RAG pipeline (pgvector)
- [ ] Web app
- [ ] Morning briefings
