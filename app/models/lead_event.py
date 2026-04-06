import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy import JSON as JSONB  # JSON works on both Postgres and SQLite
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class LeadEvent(Base):
    __tablename__ = "lead_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    lead_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("leads.id"), nullable=True
    )
    receipt_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("webhook_receipts.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: dt.datetime.now(dt.UTC)
    )

    lead = relationship("Lead", back_populates="events")
