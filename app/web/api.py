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
from sqlalchemy import select, case
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, date, timedelta
from app.db.database import AsyncSessionLocal
from app.models.task import Task, TaskStatus
from app.models.task_list import TaskList
from app.models.task_step import TaskStep
from app.models.reminder import Reminder, ReminderType, RecurrencePattern
from app.models.inbox import InboxItem
from app.models.memory import MemoryItem, MemoryType
from app.services.search_service import semantic_search

logger = logging.getLogger(__name__)

app = FastAPI(title="Pantera", docs_url=None, redoc_url=None)


# --- Pydantic schemas ---
class TaskCreate(BaseModel):
    title: str
    notes: Optional[str] = None
    status: TaskStatus = TaskStatus.NOT_STARTED
    due_date: Optional[str] = None
    project: Optional[str] = None
    parent_id: Optional[int] = None
    is_important: bool = False
    priority: str = "none"
    list_id: Optional[int] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[TaskStatus] = None
    my_day: Optional[bool] = None
    my_day_date: Optional[str] = None
    due_date: Optional[str] = None
    project: Optional[str] = None
    is_important: Optional[bool] = None
    priority: Optional[str] = None
    list_id: Optional[int] = None


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
    parent_id: Optional[int] = None
    is_important: bool = False
    priority: str = "none"
    list_id: Optional[int] = None

    model_config = {"from_attributes": True}


class TaskListCreate(BaseModel):
    name: str
    icon: Optional[str] = None
    color: Optional[str] = None
    position: int = 0


class TaskListUpdate(BaseModel):
    name: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    position: Optional[int] = None


class TaskListResponse(BaseModel):
    id: int
    name: str
    icon: Optional[str]
    color: Optional[str]
    position: int
    created_at: str

    model_config = {"from_attributes": True}


class TaskStepCreate(BaseModel):
    title: str
    position: int = 0


class TaskStepUpdate(BaseModel):
    title: Optional[str] = None
    is_completed: Optional[bool] = None
    position: Optional[int] = None


class TaskStepResponse(BaseModel):
    id: int
    task_id: int
    title: str
    is_completed: bool
    position: int

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


def task_to_response(t: Task) -> TaskResponse:
    return TaskResponse(
        id=t.id,
        title=t.title,
        notes=t.notes,
        status=t.status.value,
        due_date=t.due_date.isoformat() if t.due_date else None,
        project=t.project,
        created_at=t.created_at.isoformat() if t.created_at else None,
        my_day=t.my_day,
        my_day_date=t.my_day_date.isoformat() if t.my_day_date else None,
        parent_id=t.parent_id,
        is_important=t.is_important,
        priority=t.priority,
        list_id=t.list_id,
    )


_PRIORITY_ORDER = case(
    (Task.priority == 'high', 0),
    (Task.priority == 'medium', 1),
    (Task.priority == 'low', 2),
    else_=3
)

def build_task_list_query(
    status: Optional[TaskStatus],
    parent_id: Optional[int],
    my_day: Optional[bool],
    list_id: Optional[int] = None,
    is_important: Optional[bool] = None,
    sort: Optional[str] = None,
):
    """Build task list query with correct parent filtering semantics."""
    q = select(Task)
    if parent_id is None:
        q = q.where(Task.parent_id.is_(None))
    else:
        q = q.where(Task.parent_id == parent_id)
    if status is not None:
        q = q.where(Task.status == status)
    if my_day is True:
        q = q.where(Task.my_day == True)
    if list_id is not None:
        q = q.where(Task.list_id == list_id)
    if is_important is True:
        q = q.where(Task.is_important == True)
    if sort == 'due_date':
        q = q.order_by(Task.due_date.asc().nullslast())
    elif sort == 'priority':
        q = q.order_by(_PRIORITY_ORDER)
    elif sort == 'important':
        q = q.order_by(Task.is_important.desc())
    elif sort == 'alpha':
        q = q.order_by(Task.title.asc())
    else:
        q = q.order_by(Task.created_at.desc())
    return q


def build_inbox_list_query(is_processed: Optional[bool]):
    """Build inbox list query; return all items when no filter is provided."""
    q = select(InboxItem)
    if is_processed is not None:
        q = q.where(InboxItem.is_processed == is_processed)
    return q.order_by(InboxItem.created_at.desc())


# --- API routes ---
@app.get("/api/tasks", response_model=list[TaskResponse])
async def list_tasks(
    status: Optional[TaskStatus] = None,
    parent_id: Optional[int] = None,
    my_day: Optional[bool] = None,
    list_id: Optional[int] = None,
    is_important: Optional[bool] = None,
    sort: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """List tasks, optionally filtered by status, my_day, list_id, or is_important. Excludes subtasks by default."""
    q = build_task_list_query(status=status, parent_id=parent_id, my_day=my_day, list_id=list_id, is_important=is_important, sort=sort)
    result = await db.execute(q)
    tasks = result.scalars().all()
    return [task_to_response(t) for t in tasks]


@app.post("/api/tasks", response_model=TaskResponse)
async def create_task(body: TaskCreate, db: AsyncSession = Depends(get_db)):
    """Create a new task."""
    from app.services.task_service import parse_due_date
    due_date = None
    if body.due_date:
        try:
            due_date = datetime.fromisoformat(body.due_date.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            due_date = parse_due_date(body.due_date)
    task = Task(
        title=body.title,
        notes=body.notes,
        status=body.status,
        parent_id=body.parent_id,
        due_date=due_date,
        project=body.project,
        is_important=body.is_important,
        priority=body.priority,
        list_id=body.list_id,
    )
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
    return task_to_response(task)


@app.patch("/api/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: int,
    body: TaskUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a task (e.g. status for Kanban drag-drop, my_day, priority, importance)."""
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
    if body.due_date is not None:
        from app.services.task_service import parse_due_date
        try:
            task.due_date = datetime.fromisoformat(body.due_date.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            task.due_date = parse_due_date(body.due_date)
    if body.project is not None:
        task.project = body.project
    if body.is_important is not None:
        task.is_important = body.is_important
    if body.priority is not None:
        task.priority = body.priority
    if body.list_id is not None:
        task.list_id = body.list_id
    if body.title is not None or body.notes is not None:
        text_to_embed = f"{task.title} {task.notes or ''}".strip()
        if text_to_embed:
            from app.services.embedding_service import embed_text
            emb = await embed_text(text_to_embed)
            if emb:
                task.embedding = emb
    await db.flush()
    await db.refresh(task)
    return task_to_response(task)


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a task."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.delete(task)
    return {"ok": True}


# --- Task Steps ---
@app.get("/api/tasks/{task_id}/steps", response_model=list[TaskStepResponse])
async def list_steps(task_id: int, db: AsyncSession = Depends(get_db)):
    """List checklist steps for a task."""
    q = select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.position, TaskStep.created_at)
    result = await db.execute(q)
    steps = result.scalars().all()
    return [TaskStepResponse(id=s.id, task_id=s.task_id, title=s.title, is_completed=s.is_completed, position=s.position) for s in steps]


@app.post("/api/tasks/{task_id}/steps", response_model=TaskStepResponse)
async def create_step(task_id: int, body: TaskStepCreate, db: AsyncSession = Depends(get_db)):
    """Add a checklist step to a task."""
    step = TaskStep(task_id=task_id, title=body.title, position=body.position)
    db.add(step)
    await db.flush()
    await db.refresh(step)
    return TaskStepResponse(id=step.id, task_id=step.task_id, title=step.title, is_completed=step.is_completed, position=step.position)


@app.patch("/api/tasks/{task_id}/steps/{step_id}", response_model=TaskStepResponse)
async def update_step(task_id: int, step_id: int, body: TaskStepUpdate, db: AsyncSession = Depends(get_db)):
    """Update a checklist step (toggle, rename, reorder)."""
    result = await db.execute(select(TaskStep).where(TaskStep.id == step_id, TaskStep.task_id == task_id))
    step = result.scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    if body.title is not None:
        step.title = body.title
    if body.is_completed is not None:
        step.is_completed = body.is_completed
    if body.position is not None:
        step.position = body.position
    await db.flush()
    await db.refresh(step)
    return TaskStepResponse(id=step.id, task_id=step.task_id, title=step.title, is_completed=step.is_completed, position=step.position)


@app.delete("/api/tasks/{task_id}/steps/{step_id}")
async def delete_step(task_id: int, step_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a checklist step."""
    result = await db.execute(select(TaskStep).where(TaskStep.id == step_id, TaskStep.task_id == task_id))
    step = result.scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    await db.delete(step)
    return {"ok": True}


# --- Task Lists ---
@app.get("/api/lists", response_model=list[TaskListResponse])
async def list_task_lists(db: AsyncSession = Depends(get_db)):
    """List all task lists."""
    q = select(TaskList).order_by(TaskList.position, TaskList.created_at)
    result = await db.execute(q)
    lists = result.scalars().all()
    return [TaskListResponse(id=l.id, name=l.name, icon=l.icon, color=l.color, position=l.position, created_at=l.created_at.isoformat() if l.created_at else "") for l in lists]


@app.post("/api/lists", response_model=TaskListResponse)
async def create_task_list(body: TaskListCreate, db: AsyncSession = Depends(get_db)):
    """Create a new task list."""
    tl = TaskList(name=body.name, icon=body.icon, color=body.color, position=body.position)
    db.add(tl)
    await db.flush()
    await db.refresh(tl)
    return TaskListResponse(id=tl.id, name=tl.name, icon=tl.icon, color=tl.color, position=tl.position, created_at=tl.created_at.isoformat() if tl.created_at else "")


@app.patch("/api/lists/{list_id}", response_model=TaskListResponse)
async def update_task_list(list_id: int, body: TaskListUpdate, db: AsyncSession = Depends(get_db)):
    """Rename or reorder a task list."""
    result = await db.execute(select(TaskList).where(TaskList.id == list_id))
    tl = result.scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="List not found")
    if body.name is not None:
        tl.name = body.name
    if body.icon is not None:
        tl.icon = body.icon
    if body.color is not None:
        tl.color = body.color
    if body.position is not None:
        tl.position = body.position
    await db.flush()
    await db.refresh(tl)
    return TaskListResponse(id=tl.id, name=tl.name, icon=tl.icon, color=tl.color, position=tl.position, created_at=tl.created_at.isoformat() if tl.created_at else "")


@app.delete("/api/lists/{list_id}")
async def delete_task_list(list_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a task list (tasks become unassigned)."""
    result = await db.execute(select(TaskList).where(TaskList.id == list_id))
    tl = result.scalar_one_or_none()
    if not tl:
        raise HTTPException(status_code=404, detail="List not found")
    await db.delete(tl)
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
    is_processed: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
):
    """List inbox items; pass is_processed to filter, omit to return all."""
    q = build_inbox_list_query(is_processed=is_processed)
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

    if m.event_date and m.memory_type in {MemoryType.BIRTHDAY, MemoryType.ANNUAL_EVENT}:
        from app.services.reminder_service import create_reminder

        await create_reminder(
            content=m.content,
            trigger_at_str=m.event_date.strftime("%Y-%m-%d"),
            reminder_type="recurring",
            recurrence_pattern="yearly",
            recurrence_config={"source": "memory", "memory_id": m.id},
        )

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

    if m.event_date and m.memory_type in {MemoryType.BIRTHDAY, MemoryType.ANNUAL_EVENT}:
        from app.services.reminder_service import create_reminder

        await create_reminder(
            content=m.content,
            trigger_at_str=m.event_date.strftime("%Y-%m-%d"),
            reminder_type="recurring",
            recurrence_pattern="yearly",
            recurrence_config={"source": "memory", "memory_id": m.id},
        )

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
    return await semantic_search(db, q)


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
