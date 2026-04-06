"""Magic link authentication service."""

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import secrets

from sqlalchemy.orm import Session

from app.config import settings
from app.models import MagicLinkToken, ConsoleSession, Tenant

log = logging.getLogger(__name__)


def _utcnow() -> dt.datetime:
    """Return a naive UTC datetime (matches what PostgreSQL DateTime columns store)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


COOKIE_NAME = "angi_session"
TOKEN_PREFIX = "ml_"
SESSION_PREFIX = "sess_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _sign_cookie(payload: dict) -> str:
    """Sign a cookie payload with HMAC-SHA256."""
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(settings.session_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def _verify_cookie(cookie_value: str) -> dict | None:
    """Verify and decode a signed cookie. Returns payload or None."""
    try:
        raw, sig = cookie_value.rsplit(".", 1)
        expected = hmac.new(settings.session_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw))
        if payload.get("exp", 0) < _utcnow().timestamp() * 1000:
            return None
        return payload
    except Exception:
        return None


def create_magic_link(db: Session, email: str) -> tuple[str, str | None]:
    """Create a magic link token and return (raw_token, tenant_name).

    The raw token is what goes in the URL. We store only the hash.
    """
    # Find tenant by email (check tenant.email field)
    tenant = db.query(Tenant).filter(Tenant.email == email).first()
    tenant_name = tenant.name if tenant else None

    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_hash = _hash(raw_token)
    expires_at = _utcnow() + dt.timedelta(minutes=settings.magic_link_ttl_minutes)

    record = MagicLinkToken(
        email=email,
        tenant_id=tenant.id if tenant else None,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(record)
    db.commit()

    link = f"{settings.app_url}/auth/callback?token={raw_token}"
    log.info("Magic link created for %s (tenant=%s)", email, tenant_name)
    return link, tenant_name


def consume_magic_link(db: Session, raw_token: str) -> ConsoleSession | None:
    """Validate a magic link token and create a session.

    Returns a ConsoleSession on success, None on failure.
    """
    token_hash = _hash(raw_token)
    record = db.query(MagicLinkToken).filter(MagicLinkToken.token_hash == token_hash).first()

    if not record:
        log.warning("Magic link not found")
        return None
    if record.consumed_at is not None:
        log.warning("Magic link already consumed: %s", record.id)
        return None
    if record.expires_at < _utcnow():
        log.warning("Magic link expired: %s", record.id)
        return None

    # Mark as consumed
    record.consumed_at = _utcnow()

    # Create session
    raw_session = SESSION_PREFIX + secrets.token_urlsafe(32)
    session_hash = _hash(raw_session)
    expires_at = _utcnow() + dt.timedelta(days=settings.session_ttl_days)

    session = ConsoleSession(
        tenant_id=record.tenant_id,
        email=record.email,
        session_token_hash=session_hash,
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()

    # Build cookie value
    cookie_payload = {
        "token": raw_session,
        "email": record.email,
        "tenant_id": record.tenant_id,
        "exp": int(expires_at.timestamp() * 1000),
    }
    session._cookie_value = _sign_cookie(cookie_payload)
    session._raw_token = raw_session

    log.info("Session created for %s (session=%s)", record.email, session.id)
    return session


def validate_session(db: Session, cookie_value: str) -> ConsoleSession | None:
    """Validate a session cookie. Returns the session or None."""
    payload = _verify_cookie(cookie_value)
    if not payload:
        return None

    raw_token = payload.get("token", "")
    token_hash = _hash(raw_token)

    session = db.query(ConsoleSession).filter(
        ConsoleSession.session_token_hash == token_hash
    ).first()

    if not session:
        return None
    if session.revoked_at is not None:
        return None
    if session.expires_at < _utcnow():
        return None

    # Touch last_seen
    session.last_seen_at = _utcnow()
    db.commit()

    return session


def revoke_session(db: Session, cookie_value: str) -> bool:
    """Revoke the session identified by the cookie."""
    payload = _verify_cookie(cookie_value)
    if not payload:
        return False

    raw_token = payload.get("token", "")
    token_hash = _hash(raw_token)

    session = db.query(ConsoleSession).filter(
        ConsoleSession.session_token_hash == token_hash
    ).first()
    if session:
        session.revoked_at = _utcnow()
        db.commit()
        return True
    return False
