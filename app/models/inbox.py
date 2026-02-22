from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Text, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from .base import Base, TimestampMixin


class InboxItem(Base, TimestampMixin):
    __tablename__ = "inbox_items"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    raw_content: Mapped[str] = mapped_column(Text)
    
    # Processed content (transcription for voice, extracted text for images)
    processed_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Classification result
    classification: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    classification_confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    extracted_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    
    # Status
    is_processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    needs_clarification: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Source tracking
    source_type: Mapped[str] = mapped_column(String(50), default="text")  # text, voice, image, forward
    telegram_message_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    telegram_file_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    
    # Vector embedding for RAG
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(1536), nullable=True)
    
    def __repr__(self):
        return f"<InboxItem {self.id}: {self.source_type}>"
