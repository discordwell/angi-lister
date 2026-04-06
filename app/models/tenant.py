import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy import JSON as JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    brand_color: Mapped[str] = mapped_column(String, default="#2563eb")
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    timezone: Mapped[str] = mapped_column(String, default="America/New_York")
    email_from_name: Mapped[str | None] = mapped_column(String, nullable=True)
    intro_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    personalization_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    pricing_tiers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.UTC)
    )

    mappings = relationship("AngiMapping", back_populates="tenant")
    leads = relationship("Lead", back_populates="tenant")
    home_bases = relationship("TenantHomeBase", back_populates="tenant")
    job_rules = relationship("TenantJobRule", back_populates="tenant")
    specials = relationship("TenantSpecial", back_populates="tenant")
