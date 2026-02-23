"""
Pantera Web API - Tasks and Kanban.
"""
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.models.task import Task, TaskStatus

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


class TaskResponse(BaseModel):
    id: int
    title: str
    notes: Optional[str]
    status: str
    due_date: Optional[str]
    project: Optional[str]
    created_at: str

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
    db: AsyncSession = Depends(get_db),
):
    """List tasks, optionally filtered by status. Excludes subtasks by default."""
    q = select(Task).where(Task.parent_id.is_(None))
    if status is not None:
        q = q.where(Task.status == status)
    if parent_id is not None:
        q = q.where(Task.parent_id == parent_id)
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
        )
        for t in tasks
    ]


@app.post("/api/tasks", response_model=TaskResponse)
async def create_task(body: TaskCreate, db: AsyncSession = Depends(get_db)):
    """Create a new task."""
    task = Task(title=body.title, notes=body.notes, status=body.status)
    db.add(task)
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
    )


@app.patch("/api/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: int,
    body: TaskUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a task (e.g. status for Kanban drag-drop)."""
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


# --- Static files & SPA ---
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")
