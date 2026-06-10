from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

_TENANT_KEY = "rls_tenant"
_TENANT_LISTENER_KEY = "rls_tenant_listener"

_SET_TENANT_STMT = text("SET LOCAL app.current_tenant = :tid")


def set_tenant(db: Session, tenant_id: str, *, session_scope: bool = False) -> None:
    """Set the RLS tenant context for a session.

    By default issues a one-shot SET LOCAL: the context lasts until the current
    transaction ends (commit/rollback), after which reads fail closed (RLS
    hides every row). Handlers using this mode must build their response data
    before committing.

    With session_scope=True the context is re-applied — still via SET LOCAL —
    at the start of every transaction for the lifetime of this Session. Use
    this for sessions that commit mid-work (console, worker, bypass). Because
    the setting is always transaction-local, nothing persists on the underlying
    DBAPI connection, so tenant context can never leak across requests through
    the connection pool. Re-pinning the same tenant is a no-op; pinning a
    different tenant replaces the previous scope.

    No-op on SQLite (no RLS).
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return

    if session_scope:
        if db.info.get(_TENANT_KEY) == tenant_id:
            return
        previous = db.info.pop(_TENANT_LISTENER_KEY, None)
        if previous is not None:
            event.remove(db, "after_begin", previous)

        def _apply_tenant(session, transaction, connection):
            connection.execute(_SET_TENANT_STMT, {"tid": tenant_id})

        event.listen(db, "after_begin", _apply_tenant)
        db.info[_TENANT_KEY] = tenant_id
        db.info[_TENANT_LISTENER_KEY] = _apply_tenant

    # Apply to the transaction already in progress (after_begin only fires for
    # transactions that begin later).
    db.execute(_SET_TENANT_STMT, {"tid": tenant_id})


def get_db():
    """Unscoped DB session — legacy, prefer get_bypass_db or get_console_db."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_bypass_db():
    """DB session that bypasses RLS — for webhook handler, worker, auth, system.

    Uses session_scope=True so the bypass context is re-applied on every
    transaction and survives mid-request commits. The session is
    created/destroyed per-request so the setting doesn't leak between requests.
    """
    db = SessionLocal()
    try:
        set_tenant(db, "__bypass__", session_scope=True)
        yield db
    finally:
        db.close()


