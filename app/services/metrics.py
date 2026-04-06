"""Metrics service — computes dashboard KPIs from lead events and outbound messages."""

import datetime as dt
import logging
from statistics import median

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Lead, LeadEvent, OutboundMessage, DuplicateMatch, WebhookReceipt

log = logging.getLogger(__name__)


def get_metrics_summary(db: Session, tenant_id: str | None = None) -> dict:
    """Compute the MetricsSummary fields for the console dashboard.

    Returns a dict matching the MetricsSummary schema:
      - total_leads_24h
      - total_leads_all
      - median_speed_to_lead_seconds
      - delivery_success_rate
      - duplicate_rate
      - unmapped_count
      - parse_failure_count
    """

    now = dt.datetime.now(dt.UTC)
    h24_ago = now - dt.timedelta(hours=24)

    # Base filter for tenant scoping
    def _lead_q():
        q = db.query(func.count(Lead.id))
        if tenant_id:
            q = q.filter(Lead.tenant_id == tenant_id)
        return q

    # Total leads
    total_leads_all: int = _lead_q().scalar() or 0
    total_leads_24h: int = _lead_q().filter(Lead.created_at >= h24_ago).scalar() or 0

    # Unmapped leads
    unmapped_count: int = _lead_q().filter(Lead.status == "unmapped").scalar() or 0

    # Parse failures (receipts that failed parsing)
    pf_q = db.query(func.count(WebhookReceipt.id)).filter(WebhookReceipt.parse_valid == False)  # noqa: E712
    if tenant_id:
        pf_q = pf_q.filter(WebhookReceipt.tenant_id == tenant_id)
    parse_failure_count: int = pf_q.scalar() or 0

    # Duplicate rate
    dup_q = db.query(func.count(DuplicateMatch.id))
    if tenant_id:
        dup_q = dup_q.filter(DuplicateMatch.tenant_id == tenant_id)
    dup_count: int = dup_q.scalar() or 0
    duplicate_rate: float | None = None
    if total_leads_all > 0:
        duplicate_rate = round(dup_count / total_leads_all, 4)

    # Delivery success rate (sent vs total outbound messages, excluding simulated)
    msg_q = db.query(func.count(OutboundMessage.id)).filter(OutboundMessage.is_simulated == False)  # noqa: E712
    if tenant_id:
        msg_q = msg_q.filter(OutboundMessage.tenant_id == tenant_id)
    total_messages: int = msg_q.scalar() or 0
    sent_messages: int = msg_q.filter(OutboundMessage.status == "sent").scalar() or 0
    delivery_success_rate: float | None = None
    if total_messages > 0:
        delivery_success_rate = round(sent_messages / total_messages, 4)

    # Median speed-to-lead: time between lead_created event and email_sent event
    # for each lead that has both events.
    speed_to_lead_seconds: list[float] = []
    stl_q = db.query(Lead.id).filter(Lead.tenant_id.isnot(None))
    if tenant_id:
        stl_q = stl_q.filter(Lead.tenant_id == tenant_id)
    leads_with_events = stl_q.all()
    lead_ids = [row[0] for row in leads_with_events]

    if lead_ids:
        # Fetch lead_created and email_sent events for these leads
        created_events = (
            db.query(LeadEvent.lead_id, LeadEvent.created_at)
            .filter(
                LeadEvent.lead_id.in_(lead_ids),
                LeadEvent.event_type == "lead_created",
            )
            .all()
        )
        sent_events = (
            db.query(LeadEvent.lead_id, LeadEvent.created_at)
            .filter(
                LeadEvent.lead_id.in_(lead_ids),
                LeadEvent.event_type == "email_sent",
            )
            .all()
        )

        created_map = {row[0]: row[1] for row in created_events}
        sent_map = {row[0]: row[1] for row in sent_events}

        for lid in created_map:
            if lid in sent_map:
                delta = (sent_map[lid] - created_map[lid]).total_seconds()
                if delta >= 0:
                    speed_to_lead_seconds.append(delta)

    median_speed: float | None = None
    if speed_to_lead_seconds:
        median_speed = round(median(speed_to_lead_seconds), 2)

    # Conversion rate: (booked + won) / (mapped + booked + won + lost)
    outcome_statuses = ["mapped", "booked", "won", "lost"]
    conv_q = db.query(func.count(Lead.id))
    if tenant_id:
        conv_q = conv_q.filter(Lead.tenant_id == tenant_id)
    actionable_count: int = conv_q.filter(Lead.status.in_(outcome_statuses)).scalar() or 0
    converted_count: int = conv_q.filter(Lead.status.in_(["booked", "won"])).scalar() or 0
    conversion_rate: float | None = None
    if actionable_count > 0:
        conversion_rate = round(converted_count / actionable_count, 4)

    return {
        "total_leads_24h": total_leads_24h,
        "total_leads_all": total_leads_all,
        "median_speed_to_lead_seconds": median_speed,
        "delivery_success_rate": delivery_success_rate,
        "duplicate_rate": duplicate_rate,
        "unmapped_count": unmapped_count,
        "parse_failure_count": parse_failure_count,
        "conversion_rate": conversion_rate,
    }


def get_recent_leads(
    db: Session,
    limit: int = 30,
    offset: int = 0,
    tenant_id: str | None = None,
    status_filter: str | None = None,
) -> tuple[list[dict], int]:
    """Return leads as dicts for the dashboard table with pagination.

    Returns (leads_list, total_count).
    """

    q = db.query(Lead)
    if tenant_id:
        q = q.filter(Lead.tenant_id == tenant_id)
    if status_filter == "live":
        q = q.filter(Lead.status.in_(["won", "booked"]))
    elif status_filter == "dead":
        q = q.filter(Lead.status == "lost")

    total: int = q.count()

    leads = q.order_by(Lead.created_at.desc()).offset(offset).limit(limit).all()
    results = []
    for lead in leads:
        tenant_name = lead.tenant.name if lead.tenant else None
        results.append({
            "id": lead.id,
            "correlation_id": lead.correlation_id,
            "tenant_name": tenant_name,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "email": lead.email,
            "category": lead.category,
            "urgency": lead.urgency,
            "status": lead.status,
            "created_at": lead.created_at,
        })
    return results, total


def get_daily_breakdown(db: Session, days: int = 14, tenant_id: str | None = None) -> list[dict]:
    """Return lead counts grouped by day for the last N days."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)

    q = db.query(Lead).filter(Lead.created_at >= cutoff)
    if tenant_id:
        q = q.filter(Lead.tenant_id == tenant_id)

    leads = q.all()

    # Group by date (local = UTC for now; tenant timezone could be used later)
    from collections import Counter
    day_counts: Counter[str] = Counter()
    day_live: Counter[str] = Counter()
    day_dead: Counter[str] = Counter()

    for lead in leads:
        day_key = lead.created_at.strftime("%Y-%m-%d") if lead.created_at else "unknown"
        day_counts[day_key] += 1
        if lead.status in ("won", "booked"):
            day_live[day_key] += 1
        elif lead.status == "lost":
            day_dead[day_key] += 1

    # Build sorted list with all days in range (including zeros)
    result = []
    for i in range(days):
        d = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        result.append({
            "date": d,
            "total": day_counts.get(d, 0),
            "live": day_live.get(d, 0),
            "dead": day_dead.get(d, 0),
        })
    return result


def get_lead_detail(db: Session, lead_id: str, tenant_id: str | None = None) -> dict | None:
    """Return full lead detail including events and outbound messages."""

    q = db.query(Lead).filter(Lead.id == lead_id)
    if tenant_id:
        q = q.filter(Lead.tenant_id == tenant_id)
    lead = q.first()
    if not lead:
        return None

    events = [
        {
            "id": e.id,
            "event_type": e.event_type,
            "payload": e.payload,
            "created_at": str(e.created_at),
        }
        for e in lead.events
    ]
    messages = [
        {
            "id": m.id,
            "channel": m.channel,
            "recipient": m.recipient,
            "subject": m.subject,
            "body_html": m.body_html,
            "body_text": m.body_text,
            "status": m.status,
            "attempts": m.attempts,
            "last_error": m.last_error,
            "queued_at": str(m.queued_at),
            "sent_at": str(m.sent_at) if m.sent_at else None,
            "is_simulated": m.is_simulated,
            "generation_method": m.generation_method,
        }
        for m in lead.outbound_messages
    ]
    duplicates = [
        {
            "id": d.id,
            "original_id": d.original_id,
            "score": d.score,
            "evidence": d.evidence,
            "created_at": str(d.created_at),
        }
        for d in lead.duplicate_matches
    ]

    return {
        "id": lead.id,
        "correlation_id": lead.correlation_id,
        "tenant_name": lead.tenant.name if lead.tenant else None,
        "tenant_id": lead.tenant_id,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "email": lead.email,
        "phone": lead.phone,
        "address_line1": lead.address_line1,
        "address_line2": lead.address_line2,
        "city": lead.city,
        "state": lead.state,
        "postal_code": lead.postal_code,
        "source": lead.source,
        "description": lead.description,
        "category": lead.category,
        "urgency": lead.urgency,
        "status": lead.status,
        "fingerprint": lead.fingerprint,
        "raw_payload": lead.raw_payload,
        "created_at": str(lead.created_at),
        "updated_at": str(lead.updated_at),
        "events": events,
        "outbound_messages": messages,
        "duplicate_matches": duplicates,
    }


def get_duplicate_pairs(
    db: Session,
    limit: int = 100,
    tenant_id: str | None = None,
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
) -> list[dict]:
    """Return duplicate match pairs, optionally filtered by date range."""

    q = db.query(DuplicateMatch)
    if tenant_id:
        q = q.filter(DuplicateMatch.tenant_id == tenant_id)
    if date_from:
        q = q.filter(DuplicateMatch.created_at >= date_from)
    if date_to:
        q = q.filter(DuplicateMatch.created_at < date_to)
    matches = q.order_by(DuplicateMatch.created_at.desc()).limit(limit).all()
    results = []
    for m in matches:
        lead = m.lead
        original = m.original
        results.append({
            "id": m.id,
            "lead_id": m.lead_id,
            "original_id": m.original_id,
            "score": m.score,
            "evidence": m.evidence,
            "lead_name": f"{lead.first_name} {lead.last_name}",
            "lead_email": lead.email,
            "original_name": f"{original.first_name} {original.last_name}",
            "original_email": original.email,
            "created_at": m.created_at,
        })
    return results


# ---------------------------------------------------------------------------
# Lead outcome
# ---------------------------------------------------------------------------

VALID_OUTCOMES = {"booked", "won", "lost"}
OUTCOME_VALID_FROM = {"mapped", "booked", "won", "lost"}


def set_lead_outcome(db: Session, lead_id: str, outcome: str, notes: str | None = None) -> dict:
    """Set the conversion outcome on a lead.

    Returns a dict with lead_id, status, and previous_status.
    Raises LookupError if not found, ValueError for invalid state transitions.
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"Invalid outcome: {outcome}")

    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise LookupError(f"Lead {lead_id} not found")

    if lead.status not in OUTCOME_VALID_FROM:
        raise ValueError(f"Cannot set outcome on lead with status '{lead.status}'")

    previous_status = lead.status
    lead.status = outcome

    db.add(LeadEvent(
        lead_id=lead.id,
        tenant_id=lead.tenant_id,
        event_type=f"outcome_{outcome}",
        payload={"notes": notes, "previous_status": previous_status},
    ))
    db.flush()

    log.info("Lead %s outcome set to '%s' (was '%s')", lead.id, outcome, previous_status)
    return {"lead_id": lead.id, "status": lead.status, "previous_status": previous_status}
