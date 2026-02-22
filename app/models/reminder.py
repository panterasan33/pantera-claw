from datetime import datetime
from enum import Enum
from typing import Optional, List
from sqlalchemy import String, Text, Boolean, Enum as SQLEnum, JSON
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from .base import Base, TimestampMixin


class ReminderType(str, Enum):
    ONE_OFF = "one_off"
    RECURRING = "recurring"


class RecurrencePattern(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"
    CUSTOM = "custom"  # For complex patterns


class Reminder(Base, TimestampMixin):
    __tablename__ = "reminders"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    
    reminder_type: Mapped[ReminderType] = mapped_column(SQLEnum(ReminderType))
    
    # For one-off reminders
    trigger_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    
    # For recurring reminders
    recurrence_pattern: Mapped[Optional[RecurrencePattern]] = mapped_column(
        SQLEnum(RecurrencePattern), nullable=True
    )
    recurrence_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # e.g., {"day_of_week": 1} for "every Tuesday"
    # e.g., {"day_of_month": 15} for "15th of every month"
    
    # Nudge configuration
    nudge_times: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    # If null, use global defaults
    snooze_minutes: Mapped[int] = mapped_column(default=120)
    
    # Status tracking
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_triggered: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_acknowledged: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    next_trigger: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)
    
    # For tracking current cycle acknowledgement (recurring)
    current_cycle_done: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Vector embedding for RAG
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(1536), nullable=True)
    
    # Source tracking
    source_type: Mapped[str] = mapped_column(String(50), default="text")
    telegram_message_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    
    def __repr__(self):
        return f"<Reminder {self.id}: {self.content[:30]}>"
