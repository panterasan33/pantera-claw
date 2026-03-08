from typing import Optional, List
from sqlalchemy import String, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


class TaskList(Base, TimestampMixin):
    __tablename__ = "task_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    icon: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    color: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    tasks: Mapped[List["Task"]] = relationship("Task", back_populates="task_list")  # type: ignore[name-defined]

    def __repr__(self):
        return f"<TaskList {self.id}: {self.name}>"
