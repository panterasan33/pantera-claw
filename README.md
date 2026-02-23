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

The app runs on Railway. To deploy:

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
