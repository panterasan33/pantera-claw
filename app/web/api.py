"""
Pantera Web API - Tasks and Kanban.
"""
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from telegram import Update
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, date, timedelta
from app.db.database import AsyncSessionLocal
from app.models.task import Task, TaskStatus
from app.models.reminder import Reminder, ReminderType, RecurrencePattern
from app.models.inbox import InboxItem
from app.models.memory import MemoryItem, MemoryType

logger = logging.getLogger(__name__)

app = FastAPI(title="Pantera", docs_url=None, redoc_url=None)


# --- Pydantic schemas ---
class TaskCreate(BaseModel):
    title: str
    notes: Optional[str] = None
    status: TaskStatus = TaskStatus.NOT_STARTED


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[TaskStatus] = None
    my_day: Optional[bool] = None
    my_day_date: Optional[str] = None


class TaskResponse(BaseModel):
    id: int
    title: str
    notes: Optional[str]
    status: str
    due_date: Optional[str]
    project: Optional[str]
    created_at: str
    my_day: Optional[bool] = None
    my_day_date: Optional[str] = None

    model_config = {"from_attributes": True}


class ReminderCreate(BaseModel):
    content: str
    trigger_at: Optional[str] = None
    reminder_type: str = "one_off"
    recurrence_pattern: Optional[str] = None
    recurrence_config: Optional[dict] = None
    snooze_minutes: int = 120


class ReminderUpdate(BaseModel):
    content: Optional[str] = None
    is_active: Optional[bool] = None
    snooze_minutes: Optional[int] = None  # When set, also updates next_trigger = now + snooze_minutes


class ReminderResponse(BaseModel):
    id: int
    content: str
    reminder_type: str
    trigger_at: Optional[str]
    next_trigger: Optional[str]
    is_active: bool
    created_at: Optional[str] = None

    model_config = {"from_attributes": True}


class MemoryCreate(BaseModel):
    content: str
    memory_type: str = "note"
    event_date: Optional[str] = None
    recurrence_date: Optional[dict] = None
    project: Optional[str] = None
    tags: Optional[list[str]] = None


class MemoryUpdate(BaseModel):
    content: Optional[str] = None
    memory_type: Optional[str] = None
    event_date: Optional[str] = None


class MemoryResponse(BaseModel):
    id: int
    content: str
    memory_type: str
    event_date: Optional[str]
    project: Optional[str]
    created_at: Optional[str] = None

    model_config = {"from_attributes": True}


class InboxResponse(BaseModel):
    id: int
    raw_content: str
    processed_content: Optional[str]
    source_type: str
    classification: Optional[str]
    is_processed: bool
    created_at: Optional[str] = None

    model_config = {"from_attributes": True}


# --- DB dependency ---
async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# --- API routes ---
@app.get("/api/tasks", response_model=list[TaskResponse])
async def list_tasks(
    status: Optional[TaskStatus] = None,
    parent_id: Optional[int] = None,
    my_day: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
):
    """List tasks, optionally filtered by status or my_day. Excludes subtasks by default."""
    q = select(Task).where(Task.parent_id.is_(None))
    if status is not None:
        q = q.where(Task.status == status)
    if parent_id is not None:
        q = q.where(Task.parent_id == parent_id)
    if my_day is True:
        q = q.where(Task.my_day == True)
    q = q.order_by(Task.created_at.desc())
    result = await db.execute(q)
    tasks = result.scalars().all()
    return [
        TaskResponse(
            id=t.id,
            title=t.title,
            notes=t.notes,
            status=t.status.value,
            due_date=t.due_date.isoformat() if t.due_date else None,
            project=t.project,
            created_at=t.created_at.isoformat() if t.created_at else None,
            my_day=t.my_day,
            my_day_date=t.my_day_date.isoformat() if t.my_day_date else None,
        )
        for t in tasks
    ]


@app.post("/api/tasks", response_model=TaskResponse)
async def create_task(body: TaskCreate, db: AsyncSession = Depends(get_db)):
    """Create a new task."""
    task = Task(title=body.title, notes=body.notes, status=body.status)
    db.add(task)
    await db.flush()
    text_to_embed = f"{body.title} {body.notes or ''}".strip()
    if text_to_embed:
        from app.services.embedding_service import embed_text
        emb = await embed_text(text_to_embed)
        if emb:
            task.embedding = emb
    await db.flush()
    await db.refresh(task)
    return TaskResponse(
        id=task.id,
        title=task.title,
        notes=task.notes,
        status=task.status.value,
        due_date=task.due_date.isoformat() if task.due_date else None,
        project=task.project,
        created_at=task.created_at.isoformat() if task.created_at else None,
        my_day=task.my_day,
        my_day_date=task.my_day_date.isoformat() if task.my_day_date else None,
    )


@app.patch("/api/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: int,
    body: TaskUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a task (e.g. status for Kanban drag-drop, my_day)."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if body.title is not None:
        task.title = body.title
    if body.notes is not None:
        task.notes = body.notes
    if body.status is not None:
        task.status = body.status
    if body.my_day is not None:
        task.my_day = body.my_day
        task.my_day_date = datetime.now() if body.my_day else None
    if body.my_day_date is not None:
        try:
            task.my_day_date = datetime.fromisoformat(body.my_day_date.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    if body.title is not None or body.notes is not None:
        text_to_embed = f"{task.title} {task.notes or ''}".strip()
        if text_to_embed:
            from app.services.embedding_service import embed_text
            emb = await embed_text(text_to_embed)
            if emb:
                task.embedding = emb
    await db.flush()
    await db.refresh(task)
    return TaskResponse(
        id=task.id,
        title=task.title,
        notes=task.notes,
        status=task.status.value,
        due_date=task.due_date.isoformat() if task.due_date else None,
        project=task.project,
        created_at=task.created_at.isoformat() if task.created_at else None,
        my_day=task.my_day,
        my_day_date=task.my_day_date.isoformat() if task.my_day_date else None,
    )


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a task."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.delete(task)
    return {"ok": True}


# --- Reminders ---
@app.get("/api/reminders", response_model=list[ReminderResponse])
async def list_reminders(
    is_active: Optional[bool] = True,
    db: AsyncSession = Depends(get_db),
):
    """List reminders, optionally filtered by is_active."""
    q = select(Reminder)
    if is_active is not None:
        q = q.where(Reminder.is_active == is_active)
    q = q.order_by(Reminder.next_trigger.asc(), Reminder.created_at.desc())
    result = await db.execute(q)
    reminders = result.scalars().all()
    return [
        ReminderResponse(
            id=r.id,
            content=r.content,
            reminder_type=r.reminder_type.value,
            trigger_at=r.trigger_at.isoformat() if r.trigger_at else None,
            next_trigger=r.next_trigger.isoformat() if r.next_trigger else None,
            is_active=r.is_active,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in reminders
    ]


@app.post("/api/reminders", response_model=ReminderResponse)
async def create_reminder_api(body: ReminderCreate):
    """Create a reminder."""
    from app.services.reminder_service import create_reminder
    rid = await create_reminder(
        content=body.content,
        trigger_at_str=body.trigger_at,
        reminder_type=body.reminder_type,
        recurrence_pattern=body.recurrence_pattern,
        recurrence_config=body.recurrence_config,
        snooze_minutes=body.snooze_minutes,
    )
    if not rid:
        raise HTTPException(status_code=400, detail="Failed to create reminder")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Reminder).where(Reminder.id == rid))
        r = result.scalar_one()
        return ReminderResponse(
            id=r.id,
            content=r.content,
            reminder_type=r.reminder_type.value,
            trigger_at=r.trigger_at.isoformat() if r.trigger_at else None,
            next_trigger=r.next_trigger.isoformat() if r.next_trigger else None,
            is_active=r.is_active,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )


@app.patch("/api/reminders/{reminder_id}", response_model=ReminderResponse)
async def update_reminder(
    reminder_id: int,
    body: ReminderUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a reminder (mark done, snooze, etc)."""
    result = await db.execute(select(Reminder).where(Reminder.id == reminder_id))
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Reminder not found")
    if body.content is not None:
        r.content = body.content
    if body.is_active is not None:
        r.is_active = body.is_active
    if body.snooze_minutes is not None:
        r.snooze_minutes = body.snooze_minutes
        r.next_trigger = datetime.now() + timedelta(minutes=body.snooze_minutes)
    await db.flush()
    await db.refresh(r)
    return ReminderResponse(
        id=r.id,
        content=r.content,
        reminder_type=r.reminder_type.value,
        trigger_at=r.trigger_at.isoformat() if r.trigger_at else None,
        next_trigger=r.next_trigger.isoformat() if r.next_trigger else None,
        is_active=r.is_active,
        created_at=r.created_at.isoformat() if r.created_at else None,
    )


@app.delete("/api/reminders/{reminder_id}")
async def delete_reminder(reminder_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a reminder."""
    result = await db.execute(select(Reminder).where(Reminder.id == reminder_id))
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Reminder not found")
    await db.delete(r)
    return {"ok": True}


# --- Inbox ---
@app.get("/api/inbox", response_model=list[InboxResponse])
async def list_inbox(
    is_processed: Optional[bool] = False,
    db: AsyncSession = Depends(get_db),
):
    """List inbox items, default unprocessed only."""
    q = select(InboxItem)
    if is_processed is not None:
        q = q.where(InboxItem.is_processed == is_processed)
    q = q.order_by(InboxItem.created_at.desc())
    result = await db.execute(q)
    items = result.scalars().all()
    return [
        InboxResponse(
            id=i.id,
            raw_content=i.raw_content,
            processed_content=i.processed_content,
            source_type=i.source_type,
            classification=i.classification,
            is_processed=i.is_processed,
            created_at=i.created_at.isoformat() if i.created_at else None,
        )
        for i in items
    ]


@app.post("/api/inbox/{item_id}/process")
async def process_inbox_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Mark inbox item as processed."""
    result = await db.execute(select(InboxItem).where(InboxItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    item.is_processed = True
    await db.flush()
    return {"ok": True}


# --- Memory ---
@app.get("/api/memory", response_model=list[MemoryResponse])
async def list_memory(db: AsyncSession = Depends(get_db)):
    """List memory items."""
    q = select(MemoryItem).order_by(MemoryItem.created_at.desc())
    result = await db.execute(q)
    items = result.scalars().all()
    return [
        MemoryResponse(
            id=m.id,
            content=m.content,
            memory_type=m.memory_type.value,
            event_date=m.event_date.isoformat() if m.event_date else None,
            project=m.project,
            created_at=m.created_at.isoformat() if m.created_at else None,
        )
        for m in items
    ]


@app.post("/api/memory", response_model=MemoryResponse)
async def create_memory(body: MemoryCreate, db: AsyncSession = Depends(get_db)):
    """Create a memory item."""
    try:
        mt = MemoryType(body.memory_type)
    except ValueError:
        mt = MemoryType.NOTE
    event_date = None
    if body.event_date:
        s = body.event_date.strip()
        for fmt in ("%Y-%m-%d", "%m/%d", "%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                event_date = dt.date()
                if fmt in ("%m/%d", "%m-%d") and event_date.year == 1900:
                    event_date = date(date.today().year, event_date.month, event_date.day)
                break
            except ValueError:
                continue
    m = MemoryItem(
        content=body.content,
        memory_type=mt,
        event_date=event_date,
        recurrence_date=body.recurrence_date,
        project=body.project,
        tags=body.tags,
    )
    db.add(m)
    await db.flush()
    try:
        from app.services.embedding_service import embed_text
        emb = await embed_text(body.content)
        if emb:
            m.embedding = emb
        await db.flush()
    except Exception:
        pass
    await db.refresh(m)
    return MemoryResponse(
        id=m.id,
        content=m.content,
        memory_type=m.memory_type.value,
        event_date=m.event_date.isoformat() if m.event_date else None,
        project=m.project,
        created_at=m.created_at.isoformat() if m.created_at else None,
    )


@app.patch("/api/memory/{memory_id}", response_model=MemoryResponse)
async def update_memory(
    memory_id: int,
    body: MemoryUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a memory item."""
    result = await db.execute(select(MemoryItem).where(MemoryItem.id == memory_id))
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Memory not found")
    if body.content is not None:
        m.content = body.content
    if body.memory_type is not None:
        try:
            m.memory_type = MemoryType(body.memory_type)
        except ValueError:
            pass
    if body.event_date is not None:
        try:
            m.event_date = datetime.strptime(body.event_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass
    await db.flush()
    await db.refresh(m)
    return MemoryResponse(
        id=m.id,
        content=m.content,
        memory_type=m.memory_type.value,
        event_date=m.event_date.isoformat() if m.event_date else None,
        project=m.project,
        created_at=m.created_at.isoformat() if m.created_at else None,
    )


@app.delete("/api/memory/{memory_id}")
async def delete_memory(memory_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a memory item."""
    result = await db.execute(select(MemoryItem).where(MemoryItem.id == memory_id))
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Memory not found")
    await db.delete(m)
    return {"ok": True}


# --- Search (RAG) ---
@app.get("/api/search")
async def search_api(q: str = "", db: AsyncSession = Depends(get_db)):
    """Semantic search over tasks, reminders, memory."""
    if not q or not q.strip():
        return []
    from app.services.embedding_service import embed_text
    emb = await embed_text(q.strip())
    if not emb:
        return []

    limit_per = 7
    results = []

    # Search tasks
    q_tasks = (
        select(Task)
        .where(Task.embedding.isnot(None))
        .order_by(Task.embedding.cosine_distance(emb))
        .limit(limit_per)
    )
    r = await db.execute(q_tasks)
    for t in r.scalars().all():
        results.append({"type": "task", "id": t.id, "title": t.title, "content": t.title})

    # Search reminders
    q_rem = (
        select(Reminder)
        .where(Reminder.embedding.isnot(None))
        .where(Reminder.is_active == True)
        .order_by(Reminder.embedding.cosine_distance(emb))
        .limit(limit_per)
    )
    r = await db.execute(q_rem)
    for rem in r.scalars().all():
        results.append({"type": "reminder", "id": rem.id, "title": rem.content[:80], "content": rem.content})

    # Search memory
    q_mem = (
        select(MemoryItem)
        .where(MemoryItem.embedding.isnot(None))
        .order_by(MemoryItem.embedding.cosine_distance(emb))
        .limit(limit_per)
    )
    r = await db.execute(q_mem)
    for m in r.scalars().all():
        results.append({"type": "memory", "id": m.id, "title": m.content[:80], "content": m.content})

    return results[:20]


# --- Webhook (Telegram) - application set in main.py via app.state.bot_application ---
@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates; application processes them from update_queue."""
    application = getattr(request.app.state, "bot_application", None)
    if not application:
        return Response(status_code=503, content="Bot not initialized")
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return Response()


# --- Static files & SPA ---
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")
