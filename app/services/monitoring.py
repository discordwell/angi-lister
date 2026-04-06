"""Monitoring service — schema drift detection, error rate alerting, and daily health checks."""

import datetime as dt
import logging
import time

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Lead, WebhookReceipt

log = logging.getLogger(__name__)

# Module-level debounce state: alert_type -> epoch timestamp of last alert
_last_alert_time: dict[str, float] = {}


def _should_alert(alert_type: str, debounce_minutes: int = 30) -> bool:
    """Return True if enough time has passed since last alert of this type."""
    last = _last_alert_time.get(alert_type, 0.0)
    return (time.time() - last) >= debounce_minutes * 60


def check_error_rate(db: Session, window_minutes: int | None = None) -> dict | None:
    """Check parse failure count in the last N minutes.

    Returns a dict with alert details if threshold exceeded, else None.
    """
    window = window_minutes or settings.alert_window_minutes
    threshold = settings.alert_error_threshold
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=window)

    failure_count: int = (
        db.query(func.count(WebhookReceipt.id))
        .filter(
            WebhookReceipt.parse_valid == False,  # noqa: E712
            WebhookReceipt.received_at >= cutoff,
        )
        .scalar()
        or 0
    )

    if failure_count >= threshold:
        return {
            "type": "error_rate",
            "count": failure_count,
            "window_minutes": window,
            "threshold": threshold,
        }
    return None


def check_schema_drift(db: Session, window_hours: int = 24) -> dict | None:
    """Check for any schema drift in recent webhook receipts.

    Returns a dict with drift summary if found, else None.
    """
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=window_hours)

    all_recent = (
        db.query(WebhookReceipt)
        .filter(WebhookReceipt.received_at >= cutoff)
        .all()
    )
    # Filter in Python to avoid SQLite JSON NULL vs 'null' inconsistencies
    receipts = [r for r in all_recent if r.schema_drift]

    if not receipts:
        return None

    # Aggregate unique missing/extra fields across all drift reports
    all_missing: set[str] = set()
    all_extra: set[str] = set()
    addr_missing: set[str] = set()
    addr_extra: set[str] = set()

    for r in receipts:
        drift = r.schema_drift or {}
        all_missing.update(drift.get("missing_fields", []))
        all_extra.update(drift.get("extra_fields", []))
        addr = drift.get("address", {})
        addr_missing.update(addr.get("missing", []))
        addr_extra.update(addr.get("extra", []))

    summary: dict = {"receipt_count": len(receipts)}
    if all_missing:
        summary["missing_fields"] = sorted(all_missing)
    if all_extra:
        summary["extra_fields"] = sorted(all_extra)
    if addr_missing or addr_extra:
        summary["address"] = {}
        if addr_missing:
            summary["address"]["missing"] = sorted(addr_missing)
        if addr_extra:
            summary["address"]["extra"] = sorted(addr_extra)

    return {"type": "schema_drift", **summary}


def check_volume_anomaly(db: Session, quiet_hours: int = 6) -> dict | None:
    """Check if zero leads received in last N hours when historical data exists.

    Returns alert dict if anomaly detected, else None.
    """
    total_leads: int = db.query(func.count(Lead.id)).scalar() or 0
    if total_leads == 0:
        return None

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=quiet_hours)
    recent_leads: int = (
        db.query(func.count(Lead.id))
        .filter(Lead.created_at >= cutoff)
        .scalar()
        or 0
    )

    if recent_leads == 0:
        return {
            "type": "volume_anomaly",
            "quiet_hours": quiet_hours,
            "total_leads": total_leads,
        }
    return None


def send_alert(subject: str, body: str) -> bool:
    """Send an alert email via Resend to ALERT_EMAIL.

    Returns True if sent, False if not configured or failed.
    """
    if not settings.alert_email:
        log.debug("Alert email not configured — skipping alert: %s", subject)
        return False

    try:
        from app.services.email import send_email

        provider_id = send_email(
            recipient=settings.alert_email,
            subject=f"[Netic Alert] {subject}",
            body_html=f"<pre>{body}</pre>",
            body_text=body,
        )
        if provider_id:
            log.info("Alert sent: %s (provider_id=%s)", subject, provider_id)
        else:
            log.info("Alert skipped (Resend not configured): %s", subject)
        return True
    except Exception:
        log.exception("Failed to send alert: %s", subject)
        return False


def check_and_alert_parse_failure(db: Session) -> None:
    """Lightweight check called inline after a parse failure.

    Debounced at 30 minutes so we don't spam.
    """
    if not _should_alert("parse_failure", debounce_minutes=30):
        return

    alert = check_error_rate(db)
    if not alert:
        return

    _last_alert_time["parse_failure"] = time.time()

    body = (
        f"Parse failure rate exceeded threshold.\n\n"
        f"Failures in last {alert['window_minutes']} minutes: {alert['count']}\n"
        f"Threshold: {alert['threshold']}\n\n"
        f"Check the console dashboard for details."
    )
    send_alert("High Parse Failure Rate", body)


def run_daily_health_check(db: Session) -> dict:
    """Run all health checks and send summary if issues found.

    Returns a dict with check results.
    """
    results = {
        "error_rate": check_error_rate(db, window_minutes=1440),  # 24h
        "schema_drift": check_schema_drift(db, window_hours=24),
        "volume_anomaly": check_volume_anomaly(db),
    }

    issues = {k: v for k, v in results.items() if v is not None}

    if issues:
        lines = ["Daily Health Check — Issues Found", "=" * 40, ""]
        for name, detail in issues.items():
            lines.append(f"[{name.upper()}]")
            for k, v in detail.items():
                if k != "type":
                    lines.append(f"  {k}: {v}")
            lines.append("")

        body = "\n".join(lines)
        send_alert("Daily Health Check — Issues Found", body)
        log.warning("Daily health check found %d issue(s)", len(issues))
    else:
        log.info("Daily health check: all clear")

    return results
