from sqlalchemy import String, Text, Float, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class ClassificationFeedback(Base, TimestampMixin):
    __tablename__ = "classification_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_text: Mapped[str] = mapped_column(Text)
    predicted_type: Mapped[str] = mapped_column(String(50), index=True)
    corrected_type: Mapped[str] = mapped_column(String(50), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def __repr__(self):
        return f"<ClassificationFeedback {self.id}: {self.predicted_type}->{self.corrected_type}>"
