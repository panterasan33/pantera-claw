from datetime import datetime
from enum import Enum
from typing import Optional, List
from sqlalchemy import String, Text, ForeignKey, Boolean, Integer, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from .base import Base, TimestampMixin


class TaskStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    due_date: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    project: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    group: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)

    status: Mapped[TaskStatus] = mapped_column(
        SQLEnum(TaskStatus),
        default=TaskStatus.NOT_STARTED,
        index=True
    )
    my_day: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    my_day_date: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    # Priority and importance (MS Todo-style)
    is_important: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    priority: Mapped[str] = mapped_column(String(20), default="none")  # none, low, medium, high

    # Optional list grouping
    list_id: Mapped[Optional[int]] = mapped_column(ForeignKey("task_lists.id", ondelete="SET NULL"), nullable=True, index=True)
    task_list: Mapped[Optional["TaskList"]] = relationship("TaskList", back_populates="tasks")  # type: ignore[name-defined]

    # Parent task for subtasks
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    subtasks: Mapped[List["Task"]] = relationship("Task", back_populates="parent")
    parent: Mapped[Optional["Task"]] = relationship("Task", back_populates="subtasks", remote_side=[id])

    # Checklist steps (MS Todo-style)
    steps: Mapped[List["TaskStep"]] = relationship("TaskStep", back_populates="task", cascade="all, delete-orphan", order_by="TaskStep.position")  # type: ignore[name-defined]

    # Vector embedding for RAG
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(1536), nullable=True)

    # Source tracking
    source_type: Mapped[str] = mapped_column(String(50), default="text")  # text, voice, image
    telegram_message_id: Mapped[Optional[int]] = mapped_column(nullable=True)

    def __repr__(self):
        return f"<Task {self.id}: {self.title[:30]}>"
