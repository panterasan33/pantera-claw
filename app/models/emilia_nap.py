"""Emilia nap / sleep log — per-chat nap tracking (times stored as UTC, displayed UK)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class EmiliaNap(Base, TimestampMixin):
    __tablename__ = "emilia_naps"
    __table_args__ = (Index("ix_emilia_naps_chat_started", "chat_id", "sleep_started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)

    sleep_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sleep_ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<EmiliaNap {self.id} chat={self.chat_id} start={self.sleep_started_at}>"
