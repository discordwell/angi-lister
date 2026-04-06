import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class AngiMapping(Base):
    __tablename__ = "angi_account_mappings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    al_account_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False)

    tenant = relationship("Tenant", back_populates="mappings")
