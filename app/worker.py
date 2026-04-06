"""Background worker that polls for pending outbound messages and sends them.

Usage:
    python -m app.worker

The worker runs in an infinite loop, polling the outbound_messages table for
rows with status='pending'.  It processes them one at a time, committing after
each message so that partial progress is preserved on crash.

Configuration:
    WORKER_POLL_INTERVAL — seconds between poll cycles (default 1.0)
"""

import logging
import signal
import sys
import time

from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import SessionLocal, set_tenant
from app.models import OutboundMessage
from app.services.email import process_outbound_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("angi-worker")

_shutdown = False


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
        try:
            process_outbound_message(db, msg)
            db.commit()
            processed += 1
        except Exception:
            db.rollback()
            log.exception("Error processing outbound message %s", msg.id)

    return processed


def _recover_stuck_messages(db: Session) -> int:
    """Reset messages stuck in 'generating' for more than 60s back to 'pending'."""
    import datetime as dt

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=60)
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
        except Exception:
            log.exception("Unhandled error in worker cycle")
        finally:
            db.close()

        if not _shutdown:
            time.sleep(poll_interval)

    log.info("Worker stopped")


if __name__ == "__main__":
    main()
