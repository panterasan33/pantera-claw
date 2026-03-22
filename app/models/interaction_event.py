from enum import Enum
from typing import Optional, List

from pgvector.sqlalchemy import Vector
from sqlalchemy import Enum as SQLEnum, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class InteractionEventType(str, Enum):
    INGESTED = "ingested"
    ROUTED = "routed"
    CONFIRMED = "confirmed"
    RECLASSIFIED = "reclassified"
    EDIT_REQUESTED = "edit_requested"
    EDIT_APPLIED = "edit_applied"
    PROCESSING_ERROR = "processing_error"


class InteractionEvent(Base, TimestampMixin):
    __tablename__ = "interaction_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[InteractionEventType] = mapped_column(
        SQLEnum(InteractionEventType),
        index=True,
    )

    inbox_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("inbox_items.id"),
        nullable=True,
        index=True,
    )

    source_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    source_entity_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    target_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    target_entity_id: Mapped[Optional[int]] = mapped_column(nullable=True)

    summary: Mapped[str] = mapped_column(Text)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(1536), nullable=True)

    def __repr__(self):
        return f"<InteractionEvent {self.id}: {self.event_type.value}>"
