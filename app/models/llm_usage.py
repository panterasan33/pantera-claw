"""Append-only log of LLM API calls for usage dashboards."""

from typing import Optional

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class LlmUsageEvent(Base, TimestampMixin):
    __tablename__ = "llm_usage_events"
    __table_args__ = (Index("ix_llm_usage_events_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str] = mapped_column(String(128), index=True)
    operation: Mapped[str] = mapped_column(String(64), index=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    def __repr__(self) -> str:
        return f"<LlmUsageEvent {self.provider}/{self.model} {self.operation}>"
