"""API key authentication service.

Key format: angi_<8hex>_<48hex>
Auth header: Authorization: Bearer angi_<8hex>_<48hex>

Keys are stored as SHA-256 hashes. The raw key is only returned once at creation.
"""

import datetime as dt
import hashlib
import logging
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, set_tenant, get_bypass_db
from app.models import ApiKey, ConsoleSession, Tenant
from app.services.auth import COOKIE_NAME, validate_session

log = logging.getLogger(__name__)

KEY_PREFIX = "angi_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_api_key(
    db: Session,
    tenant_id: str | None,
    name: str,
    is_admin: bool = False,
) -> tuple[ApiKey, str]:
    """Create an API key. Returns (ApiKey record, raw_key).

    The raw_key is returned ONCE and never stored.
    """
    prefix_hex = secrets.token_hex(4)  # 8 chars
    random_hex = secrets.token_hex(24)  # 48 chars
    raw_key = f"{KEY_PREFIX}{prefix_hex}_{random_hex}"
    key_hash = _hash(raw_key)

    record = ApiKey(
        tenant_id=tenant_id,
        name=name,
        key_prefix=f"{KEY_PREFIX}{prefix_hex}",
        key_hash=key_hash,
        is_admin=is_admin,
    )
    db.add(record)
    db.flush()

    log.info("API key created: id=%s, prefix=%s, admin=%s", record.id, record.key_prefix, is_admin)
    return record, raw_key


def validate_api_key(db: Session, raw_key: str) -> ApiKey | None:
    """Look up and validate an API key. Returns the ApiKey or None."""
    key_hash = _hash(raw_key)
    record = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).first()

    if not record:
        return None
    if record.revoked_at is not None:
        return None

    record.last_used_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    db.flush()
    return record


# ── FastAPI dependencies ─────────────────────────────────────────────────────

def _extract_bearer_token(request: Request) -> str | None:
    """Extract Bearer token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


@dataclass
class TenantContext:
    """Injected into tenant API routes."""
    tenant: Tenant
    db: Session
    api_key: ApiKey


@dataclass
class AdminContext:
    """Injected into admin API routes."""
    email: str
    db: Session
    is_api_key: bool


def require_tenant(
    request: Request,
    db: Session = Depends(get_bypass_db),
) -> TenantContext:
    """Dependency: require a valid tenant API key.

    Returns a TenantContext with a tenant-scoped DB session.
    """
    raw_key = _extract_bearer_token(request)
    if not raw_key:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    api_key = validate_api_key(db, raw_key)
    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    if api_key.is_admin:
        raise HTTPException(status_code=403, detail="Admin keys cannot access tenant API")
    if not api_key.tenant_id:
        raise HTTPException(status_code=403, detail="Key is not bound to a tenant")

    tenant = db.query(Tenant).filter(Tenant.id == api_key.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=403, detail="Tenant not found")

    # Create a tenant-scoped DB session
    tenant_db = SessionLocal()
    set_tenant(tenant_db, tenant.id)

    return TenantContext(tenant=tenant, db=tenant_db, api_key=api_key)


def require_admin(
    request: Request,
    db: Session = Depends(get_bypass_db),
) -> AdminContext:
    """Dependency: require admin access.

    Accepts either:
    - API key with is_admin=True
    - Console session with tenant_id IS NULL (Netic operator)
    """
    # Try API key first
    raw_key = _extract_bearer_token(request)
    if raw_key:
        api_key = validate_api_key(db, raw_key)
        if not api_key:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")
        if not api_key.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")

        admin_db = SessionLocal()
        set_tenant(admin_db, "__all__")
        return AdminContext(
            email=f"api-key:{api_key.key_prefix}",
            db=admin_db,
            is_api_key=True,
        )

    # Fall back to console session
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        session = validate_session(db, cookie)
        if session and session.tenant_id is None:
            admin_db = SessionLocal()
            set_tenant(admin_db, "__all__")
            return AdminContext(
                email=session.email,
                db=admin_db,
                is_api_key=False,
            )

    raise HTTPException(status_code=401, detail="Admin authentication required")
