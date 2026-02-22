from datetime import datetime, date
from enum import Enum
from typing import Optional, List
from sqlalchemy import String, Text, Date, Enum as SQLEnum, JSON
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from .base import Base, TimestampMixin


class MemoryType(str, Enum):
    ANNUAL_EVENT = "annual_event"  # MOT, insurance renewals
    BIRTHDAY = "birthday"
    NOTE = "note"  # General reference
    DISCLOSURE = "disclosure"  # Conversational memory


class MemoryItem(Base, TimestampMixin):
    __tablename__ = "memory_items"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    
    memory_type: Mapped[MemoryType] = mapped_column(SQLEnum(MemoryType), index=True)
    
    # For annual/recurring events
    event_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # Store just month/day for recurring: {"month": 10, "day": 15}
    recurrence_date: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    
    # Lead time configuration (days before to surface)
    # Default: birthdays [7, 1, 0], annual events [28, 7, 0]
    lead_times: Mapped[Optional[List[int]]] = mapped_column(JSON, nullable=True)
    
    # Next trigger dates (calculated)
    next_triggers: Mapped[Optional[List[datetime]]] = mapped_column(JSON, nullable=True)
    
    # For notes/reference
    project: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    tags: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    
    # Original context (for disclosures)
    original_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Vector embedding for RAG
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(1536), nullable=True)
    
    # Source tracking
    source_type: Mapped[str] = mapped_column(String(50), default="text")
    telegram_message_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    
    def __repr__(self):
        return f"<MemoryItem {self.id}: {self.memory_type.value}>"
