import datetime as dt
import logging
from statistics import median

from sqlalchemy import func, case
from sqlalchemy.orm import Session

from app.models import Lead, LeadEvent, OutboundMessage, DuplicateMatch, WebhookReceipt, Tenant

log = logging.getLogger(__name__)


def _cutoff(days: int) -> dt.datetime:
    """Return a naive UTC datetime `days` ago for PostgreSQL comparison."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=days)


def _date_range(days: int) -> list[str]:
    """Return a list of ISO date strings from `days` ago to today."""
    today = dt.datetime.now(dt.UTC).replace(tzinfo=None).date()
    return [(today - dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


# ---------------------------------------------------------------------------
# Client functions
# ---------------------------------------------------------------------------


def get_lead_volume_timeseries(db: Session, days: int = 30) -> list[dict]:
    """Daily lead counts with zero-fill for contiguous Chart.js x-axis."""
    cutoff = _cutoff(days)

    rows = (
        db.query(
            func.date(Lead.created_at).label("day"),
            func.count(Lead.id).label("cnt"),
        )
        .filter(Lead.created_at >= cutoff)
        .group_by(func.date(Lead.created_at))
        .all()
    )

    counts = {str(r.day): r.cnt for r in rows}
    return [{"day": d, "count": counts.get(d, 0)} for d in _date_range(days)]


def get_conversion_funnel(db: Session, days: int = 30) -> dict:
    """Distinct lead counts per event type and median speed-to-lead."""
    cutoff = _cutoff(days)

    event_types = [
        "lead_created", "email_queued", "email_sent", "email_declined",
        "email_skipped", "outcome_booked", "outcome_won", "outcome_lost",
    ]

    rows = (
        db.query(
            LeadEvent.event_type,
            func.count(func.distinct(LeadEvent.lead_id)).label("cnt"),
        )
        .filter(LeadEvent.created_at >= cutoff)
        .group_by(LeadEvent.event_type)
        .all()
    )
    counts = {r.event_type: r.cnt for r in rows}

    # Median speed-to-lead: seconds between lead_created and email_sent per lead
    created_sub = (
        db.query(
            LeadEvent.lead_id.label("lead_id"),
            func.min(LeadEvent.created_at).label("created_ts"),
        )
        .filter(LeadEvent.event_type == "lead_created", LeadEvent.created_at >= cutoff)
        .group_by(LeadEvent.lead_id)
        .subquery()
    )
    sent_sub = (
        db.query(
            LeadEvent.lead_id.label("lead_id"),
            func.min(LeadEvent.created_at).label("sent_ts"),
        )
        .filter(LeadEvent.event_type == "email_sent", LeadEvent.created_at >= cutoff)
        .group_by(LeadEvent.lead_id)
        .subquery()
    )
    pairs = (
        db.query(created_sub.c.created_ts, sent_sub.c.sent_ts)
        .join(sent_sub, created_sub.c.lead_id == sent_sub.c.lead_id)
        .all()
    )
    deltas = [
        (p.sent_ts - p.created_ts).total_seconds()
        for p in pairs
        if p.created_ts and p.sent_ts
    ]
    median_stl = median(deltas) if deltas else None

    booked = counts.get("outcome_booked", 0)
    won = counts.get("outcome_won", 0)
    lost = counts.get("outcome_lost", 0)
    denom = booked + won + lost
    conversion_rate = (booked + won) / denom if denom > 0 else None

    result = {et: counts.get(et, 0) for et in event_types}
    result["median_speed_to_lead_seconds"] = median_stl
    result["conversion_rate"] = conversion_rate
    return result


def get_geo_category_breakdown(db: Session, days: int = 30) -> dict:
    """Top states, categories, and all urgency levels."""
    cutoff = _cutoff(days)
    base = db.query(Lead).filter(Lead.created_at >= cutoff).subquery()

    by_state = (
        db.query(base.c.state.label("label"), func.count().label("count"))
        .filter(base.c.state.isnot(None))
        .group_by(base.c.state)
        .order_by(func.count().desc())
        .limit(10)
        .all()
    )
    by_category = (
        db.query(base.c.category.label("label"), func.count().label("count"))
        .filter(base.c.category.isnot(None))
        .group_by(base.c.category)
        .order_by(func.count().desc())
        .limit(10)
        .all()
    )
    by_urgency = (
        db.query(base.c.urgency.label("label"), func.count().label("count"))
        .filter(base.c.urgency.isnot(None))
        .group_by(base.c.urgency)
        .order_by(func.count().desc())
        .all()
    )

    return {
        "by_state": [{"label": r.label, "count": r.count} for r in by_state],
        "by_category": [{"label": r.label, "count": r.count} for r in by_category],
        "by_urgency": [{"label": r.label, "count": r.count} for r in by_urgency],
    }


def get_duplicate_rebate_summary(db: Session, days: int = 30) -> dict:
    """Duplicate match stats with score distribution and rebate estimate."""
    cutoff = _cutoff(days)

    rows = (
        db.query(DuplicateMatch.score)
        .filter(DuplicateMatch.created_at >= cutoff)
        .all()
    )
    scores = [r.score for r in rows]

    total = len(scores)
    high_confidence = sum(1 for s in scores if s >= 0.7)
    medium_confidence = sum(1 for s in scores if 0.4 <= s < 0.7)

    # 10 bins: [0.0-0.1), [0.1-0.2), ... [0.9-1.0]
    buckets = [0] * 10
    for s in scores:
        idx = min(int(s * 10), 9)
        buckets[idx] += 1

    return {
        "total": total,
        "high_confidence": high_confidence,
        "medium_confidence": medium_confidence,
        "estimated_rebate_value": high_confidence * 25.0,
        "score_distribution": buckets,
    }


def get_conversion_detail(db: Session, days: int = 30) -> dict:
    """Lead status counts for the conversion detail card."""
    cutoff = _cutoff(days)

    rows = (
        db.query(Lead.status, func.count(Lead.id).label("cnt"))
        .filter(
            Lead.created_at >= cutoff,
            Lead.status.in_(["mapped", "booked", "won", "lost"]),
        )
        .group_by(Lead.status)
        .all()
    )
    counts = {r.status: r.cnt for r in rows}

    mapped = counts.get("mapped", 0)
    booked = counts.get("booked", 0)
    won = counts.get("won", 0)
    lost = counts.get("lost", 0)
    denom = booked + won + lost
    conversion_rate = (booked + won) / denom if denom > 0 else None

    return {
        "mapped": mapped,
        "booked": booked,
        "won": won,
        "lost": lost,
        "conversion_rate": conversion_rate,
        "pipeline_total": mapped + booked + won + lost,
    }


# ---------------------------------------------------------------------------
# Admin functions
# ---------------------------------------------------------------------------


def get_tenant_comparison(db: Session, days: int = 30) -> list[dict]:
    """Per-tenant KPIs for the admin comparison table."""
    cutoff = _cutoff(days)

    # Tenant lookup
    tenants = {t.id: t for t in db.query(Tenant).all()}

    # Lead counts per tenant
    lead_rows = (
        db.query(Lead.tenant_id, func.count(Lead.id).label("cnt"))
        .filter(Lead.created_at >= cutoff, Lead.tenant_id.isnot(None))
        .group_by(Lead.tenant_id)
        .all()
    )
    lead_counts = {r.tenant_id: r.cnt for r in lead_rows}

    # Speed-to-lead per tenant (median computed in Python)
    created_sub = (
        db.query(
            LeadEvent.lead_id.label("lead_id"),
            LeadEvent.tenant_id.label("tenant_id"),
            func.min(LeadEvent.created_at).label("created_ts"),
        )
        .filter(LeadEvent.event_type == "lead_created", LeadEvent.created_at >= cutoff)
        .group_by(LeadEvent.lead_id, LeadEvent.tenant_id)
        .subquery()
    )
    sent_sub = (
        db.query(
            LeadEvent.lead_id.label("lead_id"),
            func.min(LeadEvent.created_at).label("sent_ts"),
        )
        .filter(LeadEvent.event_type == "email_sent", LeadEvent.created_at >= cutoff)
        .group_by(LeadEvent.lead_id)
        .subquery()
    )
    stl_rows = (
        db.query(created_sub.c.tenant_id, created_sub.c.created_ts, sent_sub.c.sent_ts)
        .join(sent_sub, created_sub.c.lead_id == sent_sub.c.lead_id)
        .all()
    )
    stl_by_tenant: dict[str, list[float]] = {}
    for r in stl_rows:
        if r.created_ts and r.sent_ts:
            stl_by_tenant.setdefault(r.tenant_id, []).append(
                (r.sent_ts - r.created_ts).total_seconds()
            )

    # Delivery rate per tenant: sent / (sent + failed)
    delivery_rows = (
        db.query(
            OutboundMessage.tenant_id,
            OutboundMessage.status,
            func.count(OutboundMessage.id).label("cnt"),
        )
        .filter(OutboundMessage.queued_at >= cutoff, OutboundMessage.tenant_id.isnot(None))
        .group_by(OutboundMessage.tenant_id, OutboundMessage.status)
        .all()
    )
    delivery_by_tenant: dict[str, dict[str, int]] = {}
    for r in delivery_rows:
        delivery_by_tenant.setdefault(r.tenant_id, {})[r.status] = r.cnt

    # Duplicate rate per tenant: duplicates / leads
    dup_rows = (
        db.query(DuplicateMatch.tenant_id, func.count(DuplicateMatch.id).label("cnt"))
        .filter(DuplicateMatch.created_at >= cutoff)
        .group_by(DuplicateMatch.tenant_id)
        .all()
    )
    dup_counts = {r.tenant_id: r.cnt for r in dup_rows}

    # Personalization rate per tenant: llm / total non-simulated messages
    pers_rows = (
        db.query(
            OutboundMessage.tenant_id,
            OutboundMessage.generation_method,
            func.count(OutboundMessage.id).label("cnt"),
        )
        .filter(
            OutboundMessage.queued_at >= cutoff,
            OutboundMessage.is_simulated == False,  # noqa: E712
            OutboundMessage.tenant_id.isnot(None),
        )
        .group_by(OutboundMessage.tenant_id, OutboundMessage.generation_method)
        .all()
    )
    pers_by_tenant: dict[str, dict[str, int]] = {}
    for r in pers_rows:
        pers_by_tenant.setdefault(r.tenant_id, {})[r.generation_method or "unknown"] = r.cnt

    results = []
    for tid, tenant in tenants.items():
        lc = lead_counts.get(tid, 0)
        stl_list = stl_by_tenant.get(tid, [])
        speed = median(stl_list) if stl_list else None

        dstats = delivery_by_tenant.get(tid, {})
        sent = dstats.get("sent", 0)
        failed = dstats.get("failed", 0)
        d_denom = sent + failed
        delivery_rate = sent / d_denom if d_denom > 0 else None

        dup_c = dup_counts.get(tid, 0)
        duplicate_rate = dup_c / lc if lc > 0 else None

        pstats = pers_by_tenant.get(tid, {})
        p_total = sum(pstats.values())
        llm_c = pstats.get("llm", 0)
        personalization_rate = llm_c / p_total if p_total > 0 else None

        if delivery_rate is None:
            health = "good"
        elif delivery_rate > 0.9:
            health = "good"
        elif delivery_rate > 0.7:
            health = "warn"
        else:
            health = "critical"

        results.append({
            "tenant_name": tenant.name,
            "tenant_slug": tenant.slug,
            "brand_color": tenant.brand_color,
            "lead_count": lc,
            "speed_to_lead": speed,
            "delivery_rate": delivery_rate,
            "duplicate_rate": duplicate_rate,
            "personalization_rate": personalization_rate,
            "health": health,
        })

    return results


def get_system_health(db: Session) -> dict:
    """System health snapshot — always uses a 24-hour window."""
    cutoff_24h = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=24)

    parse_failures_24h = (
        db.query(func.count(WebhookReceipt.id))
        .filter(WebhookReceipt.received_at >= cutoff_24h, WebhookReceipt.parse_valid == False)  # noqa: E712
        .scalar()
    ) or 0

    schema_drift_alerts_24h = (
        db.query(func.count(WebhookReceipt.id))
        .filter(
            WebhookReceipt.received_at >= cutoff_24h,
            WebhookReceipt.schema_drift.isnot(None),
        )
        .scalar()
    ) or 0

    unmapped_count = (
        db.query(func.count(Lead.id))
        .filter(Lead.status == "unmapped")
        .scalar()
    ) or 0

    pending_queue_depth = (
        db.query(func.count(OutboundMessage.id))
        .filter(OutboundMessage.status == "pending")
        .scalar()
    ) or 0

    email_sent_24h = (
        db.query(func.count(OutboundMessage.id))
        .filter(OutboundMessage.sent_at >= cutoff_24h, OutboundMessage.status == "sent")
        .scalar()
    ) or 0

    email_failures_24h = (
        db.query(func.count(OutboundMessage.id))
        .filter(OutboundMessage.queued_at >= cutoff_24h, OutboundMessage.status == "failed")
        .scalar()
    ) or 0

    email_total_24h = email_sent_24h + email_failures_24h
    email_failure_rate_24h = email_failures_24h / email_total_24h if email_total_24h > 0 else 0.0

    if parse_failures_24h > 5 or email_failure_rate_24h > 0.1:
        overall_health = "critical"
    elif parse_failures_24h > 0 or schema_drift_alerts_24h > 0 or email_failures_24h > 0:
        overall_health = "warn"
    else:
        overall_health = "good"

    return {
        "parse_failures_24h": parse_failures_24h,
        "schema_drift_alerts_24h": schema_drift_alerts_24h,
        "unmapped_count": unmapped_count,
        "pending_queue_depth": pending_queue_depth,
        "email_failures_24h": email_failures_24h,
        "email_failure_rate_24h": email_failure_rate_24h,
        "overall_health": overall_health,
    }


def get_personalization_performance(db: Session, days: int = 30) -> dict:
    """Personalization engine metrics: methods, latency, and LLM success rate."""
    cutoff = _cutoff(days)

    # Generation method distribution (non-simulated only)
    method_rows = (
        db.query(
            OutboundMessage.generation_method,
            func.count(OutboundMessage.id).label("cnt"),
        )
        .filter(
            OutboundMessage.queued_at >= cutoff,
            OutboundMessage.is_simulated == False,  # noqa: E712
        )
        .group_by(OutboundMessage.generation_method)
        .all()
    )
    method_counts = {(r.generation_method or "unknown"): r.cnt for r in method_rows}

    # LLM latency buckets: <500ms, 500-1s, 1-2s, 2-5s, 5s+
    latency_labels = ["<500ms", "500ms-1s", "1-2s", "2-5s", "5s+"]
    latency_buckets = [0] * 5

    latency_rows = (
        db.query(OutboundMessage.llm_duration_ms)
        .filter(
            OutboundMessage.queued_at >= cutoff,
            OutboundMessage.is_simulated == False,  # noqa: E712
            OutboundMessage.llm_duration_ms.isnot(None),
        )
        .all()
    )
    durations = []
    for (ms,) in latency_rows:
        durations.append(ms)
        if ms < 500:
            latency_buckets[0] += 1
        elif ms < 1000:
            latency_buckets[1] += 1
        elif ms < 2000:
            latency_buckets[2] += 1
        elif ms < 5000:
            latency_buckets[3] += 1
        else:
            latency_buckets[4] += 1

    avg_llm_duration_ms = sum(durations) / len(durations) if durations else None

    # Declined / skipped counts from LeadEvent
    declined_count = (
        db.query(func.count(LeadEvent.id))
        .filter(LeadEvent.event_type == "email_declined", LeadEvent.created_at >= cutoff)
        .scalar()
    ) or 0

    skipped_count = (
        db.query(func.count(LeadEvent.id))
        .filter(LeadEvent.event_type == "email_skipped", LeadEvent.created_at >= cutoff)
        .scalar()
    ) or 0

    # LLM success rate: llm / (llm + jinja2_fallback)
    llm_c = method_counts.get("llm", 0)
    fallback_c = method_counts.get("jinja2_fallback", 0)
    llm_denom = llm_c + fallback_c
    llm_success_rate = llm_c / llm_denom if llm_denom > 0 else None

    return {
        "method_counts": method_counts,
        "latency_buckets": latency_buckets,
        "latency_labels": latency_labels,
        "declined_count": declined_count,
        "skipped_count": skipped_count,
        "llm_success_rate": llm_success_rate,
        "avg_llm_duration_ms": avg_llm_duration_ms,
    }


def get_platform_timeseries(db: Session, days: int = 30) -> dict:
    """Per-tenant per-day lead counts for stacked Chart.js line chart."""
    cutoff = _cutoff(days)

    tenants = {t.id: t for t in db.query(Tenant).all()}
    all_days = _date_range(days)

    rows = (
        db.query(
            Lead.tenant_id,
            func.date(Lead.created_at).label("day"),
            func.count(Lead.id).label("cnt"),
        )
        .filter(Lead.created_at >= cutoff, Lead.tenant_id.isnot(None))
        .group_by(Lead.tenant_id, func.date(Lead.created_at))
        .all()
    )

    # Build per-tenant day->count map
    tenant_data: dict[str, dict[str, int]] = {}
    for r in rows:
        tenant_data.setdefault(r.tenant_id, {})[str(r.day)] = r.cnt

    datasets = []
    for tid, tenant in tenants.items():
        day_counts = tenant_data.get(tid, {})
        datasets.append({
            "tenant_name": tenant.name,
            "brand_color": tenant.brand_color,
            "data": [day_counts.get(d, 0) for d in all_days],
        })

    return {
        "labels": all_days,
        "datasets": datasets,
    }
