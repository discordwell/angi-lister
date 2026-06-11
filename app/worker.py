"""Background worker that polls for pending outbound messages and sends them.

Usage:
    python -m app.worker

The worker runs in an infinite loop, polling the outbound_messages table for
rows with status='pending'.  It processes them one at a time, committing after
each message so that partial progress is preserved on crash.

Configuration:
    WORKER_POLL_INTERVAL — seconds between poll cycles (default 1.0)
"""

import datetime as dt
import logging
import signal
import time

from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import SessionLocal, set_tenant
from app.utils import utcnow
from app.models import LeadEvent, OutboundMessage
from app.services.email import MAX_ATTEMPTS, process_outbound_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("angi-worker")

_shutdown = False
_last_daily_check: float = 0.0


def _handle_signal(signum, _frame):
    global _shutdown
    log.info("Received signal %s — shutting down gracefully", signum)
    _shutdown = True


def fetch_pending(db: Session, batch_size: int = 10) -> list[OutboundMessage]:
    """Fetch a batch of pending messages ordered by queue time."""
    return (
        db.query(OutboundMessage)
        .filter(OutboundMessage.status == "pending")
        .order_by(OutboundMessage.queued_at.asc())
        .limit(batch_size)
        .all()
    )


def run_cycle(db: Session) -> int:
    """Process one batch of pending messages.  Returns count processed."""
    messages = fetch_pending(db)
    if not messages:
        return 0

    processed = 0
    for msg in messages:
        if _shutdown:
            break
        msg_id = msg.id  # capture before any ORM expiry
        try:
            process_outbound_message(db, msg)
            db.commit()
            processed += 1
        except Exception as exc:
            db.rollback()
            # Re-set bypass after rollback — rollback can clear SET on some drivers
            set_tenant(db, "__bypass__", session_scope=True)
            log.exception("Error processing outbound message %s", msg_id)
            _record_crashed_attempt(db, msg_id, exc)

    return processed


def _record_crashed_attempt(db: Session, msg_id: str, exc: Exception) -> None:
    """Count an attempt for a message whose processing raised.

    The rollback in run_cycle also discards the attempts increment made inside
    send_outbound_message, so without this a message that keeps raising would
    stay 'pending' with attempts=0 and be retried every poll cycle forever.
    """
    try:
        msg = db.get(OutboundMessage, msg_id)
        if msg is None or msg.status not in ("pending", "generating"):
            return
        msg.attempts += 1
        msg.last_error = f"Worker crash: {exc!r}"[:500]
        if msg.attempts >= MAX_ATTEMPTS:
            msg.status = "failed"
            db.add(LeadEvent(
                lead_id=msg.lead_id,
                tenant_id=msg.tenant_id,
                event_type="email_failed",
                payload={
                    "outbound_message_id": msg.id,
                    "attempts": msg.attempts,
                    "last_error": msg.last_error,
                },
            ))
            log.warning("Message %s failed after %d attempts", msg.id, msg.attempts)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("Could not record crashed attempt for message %s", msg_id)


def _recover_stuck_messages(db: Session) -> int:
    """Reset messages stuck in 'generating' for more than 60s back to 'pending'."""
    cutoff = utcnow() - dt.timedelta(seconds=60)
    stuck = (
        db.query(OutboundMessage)
        .filter(
            OutboundMessage.status == "generating",
            OutboundMessage.queued_at < cutoff,
        )
        .all()
    )
    for msg in stuck:
        msg.status = "pending"
        log.warning("Recovered stuck message %s from 'generating' back to 'pending'", msg.id)
    if stuck:
        db.commit()
    return len(stuck)


def _maybe_run_daily_check(db: Session) -> None:
    """Run monitoring health checks every 24 hours."""
    global _last_daily_check
    now = time.time()
    if now - _last_daily_check < 86400:
        return
    _last_daily_check = now
    try:
        from app.services.monitoring import run_daily_health_check
        results = run_daily_health_check(db)
        issues = {k: v for k, v in results.items() if v is not None}
        if issues:
            log.info("Daily health check found %d issue(s): %s", len(issues), list(issues.keys()))
        else:
            log.info("Daily health check: all clear")
    except Exception:
        log.exception("Daily health check failed")


def main() -> None:
    """Entry point — poll loop."""
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    poll_interval = settings.worker_poll_interval
    log.info(
        "Angi-Lister worker starting (poll_interval=%.1fs, resend_configured=%s)",
        poll_interval,
        bool(settings.resend_api_key),
    )

    # Recover any messages left in 'generating' state from a previous crash
    db = SessionLocal()
    set_tenant(db, "__bypass__", session_scope=True)
    try:
        recovered = _recover_stuck_messages(db)
        if recovered:
            log.info("Recovered %d stuck message(s) on startup", recovered)
    finally:
        db.close()

    while not _shutdown:
        db = SessionLocal()
        set_tenant(db, "__bypass__", session_scope=True)
        try:
            processed = run_cycle(db)
            if processed:
                log.info("Processed %d message(s) this cycle", processed)
            _maybe_run_daily_check(db)
        except Exception:
            log.exception("Unhandled error in worker cycle")
        finally:
            db.close()

        if not _shutdown:
            time.sleep(poll_interval)

    log.info("Worker stopped")


if __name__ == "__main__":
    main()
