from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _set_tenant(db: Session, tenant_id: str) -> None:
    """Set the RLS tenant context on the current transaction.

    Uses SET LOCAL so the setting is scoped to the current transaction
    and automatically reset on commit/rollback. No-op on SQLite (no RLS).
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    db.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": tenant_id})


def get_db():
    """Unscoped DB session — legacy, prefer get_bypass_db or get_console_db."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_bypass_db():
    """DB session that bypasses RLS — for webhook handler, worker, auth, system."""
    db = SessionLocal()
    try:
        _set_tenant(db, "__bypass__")
        yield db
    finally:
        db.close()


def get_admin_db():
    """DB session with admin access — sees all tenants, read-only intent."""
    db = SessionLocal()
    try:
        _set_tenant(db, "__all__")
        yield db
    finally:
        db.close()
