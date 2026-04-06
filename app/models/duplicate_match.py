import datetime as dt
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy import JSON as JSONB  # JSON works on both Postgres and SQLite
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class DuplicateMatch(Base):
    __tablename__ = "duplicate_matches"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    lead_id: Mapped[str] = mapped_column(String, ForeignKey("leads.id"), nullable=False)
    original_id: Mapped[str] = mapped_column(String, ForeignKey("leads.id"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, index=True
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.UTC)
    )

    lead = relationship("Lead", foreign_keys=[lead_id], back_populates="duplicate_matches")
    original = relationship("Lead", foreign_keys=[original_id])
