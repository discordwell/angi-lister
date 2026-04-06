from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def set_tenant(db: Session, tenant_id: str, *, session_scope: bool = False) -> None:
    """Set the RLS tenant context.

    By default uses SET LOCAL (transaction-scoped, resets on commit/rollback).
    Pass session_scope=True to use SET (connection-scoped) — use this for
    long-lived sessions that commit mid-work (e.g. the worker loop).

    No-op on SQLite (no RLS).
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    scope = "" if session_scope else "LOCAL "
    db.execute(text(f"SET {scope}app.current_tenant = :tid"), {"tid": tenant_id})


def get_db():
    """Unscoped DB session — legacy, prefer get_bypass_db or get_console_db."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_bypass_db():
    """DB session that bypasses RLS — for webhook handler, worker, auth, system.

    Uses session_scope=True (SET, not SET LOCAL) so the bypass survives
    mid-request commits. The session is created/destroyed per-request so
    the setting doesn't leak between requests.
    """
    db = SessionLocal()
    try:
        set_tenant(db, "__bypass__", session_scope=True)
        yield db
    finally:
        db.close()


