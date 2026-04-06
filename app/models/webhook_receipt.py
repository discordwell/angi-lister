import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy import JSON as JSONB  # JSON works on both Postgres and SQLite
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WebhookReceipt(Base):
    __tablename__ = "webhook_receipts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: dt.datetime.now(dt.UTC)
    )
    headers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    auth_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    parse_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_drift: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
