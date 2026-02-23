# 🐆 Pantera

Personal assistant that captures, classifies, and organizes everything you throw at it.

## Features

- **Smart Classification**: Text, voice, images → automatically sorted into tasks, reminders, notes, or memory items
- **Persistent Reminders**: Keeps nudging until you acknowledge
- **Long-term Memory**: Birthdays, MOT, renewals — surfaces them at the right time
- **RAG-powered Search**: Ask "what did I say about X?" and get answers
- **My Day**: Daily focus view with morning briefings

## Setup

1. Copy `.config/secrets.env.example` to `.config/secrets.env`
2. Add your tokens (Telegram, OpenAI, Anthropic, Railway DB from pgvector service)
3. Ensure pantera-claw service on Railway has the same env vars (or reference pgvector's)

## Deploy to Railway

The app runs on Railway. **Important**: Enable **Public Networking** on your service so the bot can use webhooks. Without it, polling mode causes "Conflict: only one bot instance" when multiple replicas or restarts overlap. With webhooks, Telegram pushes updates to your URL.

To deploy:

**Option A: Push to GitHub** (if connected)
```bash
git add .
git commit -m "Deploy"
git push
```
Railway auto-deploys on push.

**Option B: Railway Dashboard**
1. Go to [railway.app](https://railway.app) → thorough-learning → pantera-claw
2. Click **Redeploy** on the latest deployment, or connect a GitHub repo and push

**Option C: CLI** (if `railway link` works)
```bash
make deploy
# or
railway up
```

## Environment Variables

```
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id  # For reminder nudges and morning briefings
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/pantera
OPENAI_API_KEY=your_openai_key  # For Whisper, embeddings, Vision
ANTHROPIC_API_KEY=your_anthropic_key  # For classification (optional)
# WEBHOOK_URL=https://your-app.up.railway.app  # Optional; auto-detected from RAILWAY_PUBLIC_DOMAIN
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for how components work together.

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

- [x] Project structure
- [x] Database models (Task, Reminder, Memory, Inbox)
- [x] Classification engine (LLM + rule-based fallback)
- [x] Basic Telegram bot skeleton
- [x] Task CRUD
- [x] Reminder engine with nudging
- [x] Voice transcription (Whisper)
- [x] Image processing (Vision)
- [x] RAG pipeline (pgvector)
- [x] Web app (Tasks, Reminders, Inbox, Memory, Search, Planned for Today, Settings)
- [x] Morning briefings
