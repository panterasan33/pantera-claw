# Pantera Architecture

Single reference for how the app works. Update when adding changes.

---

## Overview

Pantera is a personal assistant with two entry points: **Telegram bot** and **web app**. Both share the same Postgres backend. Classification runs on text (LLM + rule-based fallback); voice and images are transcribed/extracted first, then classified.

---

## Entry Point

`main.py` → `asyncio.run(main())`:
- Loads `.config/secrets.env`
- Initializes DB (pgvector extension, tables)
- Creates Telegram bot
- Starts APScheduler (reminder nudge every minute, morning briefing at `morning_briefing_time`)
- Serves FastAPI + webhook on one port (Railway: `PORT`)

---

## Data Flow

```
Telegram → webhook → update_queue → handlers → classify → persist (Task/Reminder/Memory)
Web UI   → /api/*  → FastAPI routes → DB
```

---

## Components

| Component | Path | Purpose |
|-----------|------|---------|
| **Bot** | `app/bot/bot.py`, `handlers.py` | Commands, message/voice/photo handling, callbacks |
| **Web API** | `app/web/api.py` | REST endpoints for tasks, reminders, inbox, memory, search |
| **Scheduler** | `app/jobs/reminder_nudge.py`, `morning_briefing.py` | Background jobs |
| **Classifier** | `app/services/classifier.py` | LLM → task/reminder/note/memory/question |
| **Embeddings** | `app/services/embedding_service.py` | OpenAI `text-embedding-3-small` for RAG |
| **DB** | `app/db/database.py`, `app/models/**` | Async SQLAlchemy, pgvector |

---

## Models

| Model | Table | Key fields |
|-------|-------|------------|
| `Task` | `tasks` | title, status, my_day, my_day_date, embedding |
| `Reminder` | `reminders` | content, trigger_at, next_trigger, is_active, recurrence |
| `MemoryItem` | `memory_items` | content, memory_type, event_date |
| `InboxItem` | `inbox_items` | raw_content, processed_content, is_processed |

---

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/tasks` | List tasks (filter: status, my_day) |
| POST | `/api/tasks` | Create task |
| PATCH | `/api/tasks/{id}` | Update task (status, my_day) |
| DELETE | `/api/tasks/{id}` | Delete task |
| GET | `/api/reminders` | List reminders |
| POST | `/api/reminders` | Create reminder |
| PATCH | `/api/reminders/{id}` | Update (snooze, done) |
| DELETE | `/api/reminders/{id}` | Delete reminder |
| GET | `/api/inbox` | List unprocessed inbox items |
| POST | `/api/inbox/{id}/process` | Mark processed |
| GET | `/api/memory` | List memory |
| POST | `/api/memory` | Create memory |
| PATCH/DELETE | `/api/memory/{id}` | Update/delete |
| GET | `/api/search?q=` | Semantic search (RAG) |
| POST | `/webhook` | Telegram webhook |

---

## Web UI

Single HTML file: `app/web/static/index.html` (no build step).

- **Views:** Tasks (Kanban), Reminders, Inbox, Memory, Planned for Today, Search, Settings
- **Nav:** `data-view` attributes, JS hides/shows `.view` containers
- **Settings:** `localStorage.pantera_settings` → showNotes, showProject, showDueDate
- **Styling:** CSS vars (`--bg`, `--paper`, `--ink`, `--accent`), Noto Sans/Serif JP

---

## Bot Handlers

| Input | Handler | Flow |
|-------|---------|------|
| Text | `handle_message` | classify → persist task/reminder → reply + confirmation keyboard |
| Voice | `handle_voice` | download → Whisper → classify → persist → reply |
| Photo | `handle_photo` | download → Vision (gpt-4o-mini) → classify → persist → reply |
| Callbacks | `handle_callback` | reminder_done/snooze/tomorrow, myday, confirm_* |

---

## Background Jobs

| Job | Schedule | Action |
|-----|----------|--------|
| `reminder_nudge` | Every 1 min | Find `next_trigger <= now`, send Telegram to `TELEGRAM_CHAT_ID`, update DB |
| `morning_briefing` | Cron at `morning_briefing_time` | Aggregate My Day tasks, reminders, memory items → send to `TELEGRAM_CHAT_ID` |

---

## RAG / Search

- **Embed:** On create/update of Task, Reminder, MemoryItem (via `embedding_service`)
- **Search:** `GET /api/search?q=` → embed query → pgvector cosine distance across tasks, reminders, memory, limit 20

---

## Config

`app/config.py` reads from `.config/secrets.env`:
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (for nudges/briefings)
- `DATABASE_URL` (or `DATABASE_PUBLIC_URL`, `PGHOST`, etc.)
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`
- `WEBHOOK_URL`, `morning_briefing_time`, `nudge_times`

---

## File Layout

```
pantera-claw/
├── main.py                 # Entry, scheduler, webhook
├── app/
│   ├── bot/                # Telegram
│   ├── web/api.py          # FastAPI + static
│   ├── models/             # SQLAlchemy
│   ├── services/           # classifier, embed, task, reminder
│   ├── jobs/               # reminder_nudge, morning_briefing
│   ├── db/                 # database.py
│   └── config.py
├── .config/secrets.env
└── app/web/static/index.html
```
