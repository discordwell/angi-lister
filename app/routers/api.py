"""JSON API endpoints for the console and external integrations."""

import csv
import io
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db.session import get_bypass_db
from app.models import AngiMapping, Lead, OutboundMessage, WebhookReceipt, LeadEvent
from app.schemas.angi import AngiLeadPayload
from app.schemas.api import MetricsSummary, LeadSummary, LeadDetail, DuplicatePair, WebhookResponse
from app.services.ingestion import process_lead
from app.services.metrics import (
    get_metrics_summary,
    get_recent_leads,
    get_lead_detail,
    get_duplicate_pairs,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


@router.get("/metrics", response_model=MetricsSummary)
def api_metrics(db: Session = Depends(get_bypass_db)):
    """Return current KPI metrics."""
    data = get_metrics_summary(db)
    return MetricsSummary(**data)


@router.get("/leads", response_model=list[LeadSummary])
def api_leads(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_bypass_db),
):
    """Return recent leads (most recent first)."""
    rows = get_recent_leads(db, limit=limit)
    return [LeadSummary(**r) for r in rows]


@router.get("/leads/{lead_id}", response_model=LeadDetail)
def api_lead_detail(lead_id: str, db: Session = Depends(get_bypass_db)):
    """Return full lead detail."""
    data = get_lead_detail(db, lead_id)
    if data is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Lead not found")
    return LeadDetail(
        id=data["id"],
        correlation_id=data["correlation_id"],
        tenant_name=data["tenant_name"],
        first_name=data["first_name"],
        last_name=data["last_name"],
        email=data["email"],
        phone=data["phone"],
        category=data["category"],
        urgency=data["urgency"],
        status=data["status"],
        created_at=data["created_at"],
        address_line1=data["address_line1"],
        address_line2=data["address_line2"],
        city=data["city"],
        state=data["state"],
        postal_code=data["postal_code"],
        source=data["source"],
        description=data["description"],
        raw_payload=data["raw_payload"],
        events=data["events"],
        outbound_messages=data["outbound_messages"],
    )


@router.get("/duplicates", response_model=list[DuplicatePair])
def api_duplicates(
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_bypass_db),
):
    """Return duplicate match pairs."""
    rows = get_duplicate_pairs(db, limit=limit)
    return [DuplicatePair(**r) for r in rows]


@router.get("/duplicates/export")
def api_duplicates_export(db: Session = Depends(get_bypass_db)):
    """Download CSV of duplicate matches for rebate claims."""
    rows = get_duplicate_pairs(db, limit=10000)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "lead_id", "original_lead_id", "lead_name", "original_name",
        "lead_email", "score", "evidence_summary", "created_at",
    ])
    for r in rows:
        evidence = r.get("evidence", {})
        evidence_parts = [k for k, v in evidence.items() if v is True]
        writer.writerow([
            r["lead_id"], r["original_id"], r["lead_name"], r["original_name"],
            r["lead_email"], r["score"], "; ".join(evidence_parts), r["created_at"],
        ])

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=duplicate_leads_rebate.csv"},
    )


@router.post("/simulate", response_model=WebhookResponse)
def api_simulate(payload: AngiLeadPayload, db: Session = Depends(get_bypass_db)):
    """Fire a test lead through the full pipeline with is_simulated=True."""
    receipt = WebhookReceipt(
        headers={"x-source": "api-simulation"},
        raw_body=payload.model_dump(),
        auth_valid=True,
        correlation_id=payload.CorrelationId,
    )
    db.add(receipt)
    db.flush()

    lead = process_lead(db, receipt, payload, is_simulated=True)
    db.commit()

    return WebhookResponse(
        receipt_id=receipt.id,
        lead_id=lead.id,
        correlation_id=lead.correlation_id,
        message=f"Simulated lead {lead.id} created (status={lead.status})",
    )


@router.post("/tenants/{tenant_id}/replay-unmapped")
def api_replay_unmapped(tenant_id: str, db: Session = Depends(get_bypass_db)):
    """Replay unmapped leads after adding a tenant mapping.

    Finds leads with status='unmapped' whose ALAccountId now maps to
    the given tenant, updates them, and queues outbound messages.
    """
    # Get all AL account IDs for this tenant
    mappings = db.query(AngiMapping).filter(AngiMapping.tenant_id == tenant_id).all()
    if not mappings:
        raise HTTPException(status_code=404, detail="No mappings found for this tenant")

    al_ids = [m.al_account_id for m in mappings]
    tenant = mappings[0].tenant

    # Find unmapped leads matching these AL account IDs
    unmapped = (
        db.query(Lead)
        .filter(Lead.status == "unmapped", Lead.al_account_id.in_(al_ids))
        .all()
    )

    replayed = 0
    for lead in unmapped:
        lead.tenant_id = tenant_id
        lead.status = "mapped"

        db.add(LeadEvent(
            lead_id=lead.id,
            tenant_id=tenant_id,
            event_type="replayed",
            payload={"tenant_id": tenant_id, "tenant_name": tenant.name},
        ))
        db.add(LeadEvent(
            lead_id=lead.id,
            tenant_id=tenant_id,
            event_type="tenant_mapped",
            payload={"tenant_id": tenant_id, "tenant_name": tenant.name},
        ))

        # Queue outbound message
        msg = OutboundMessage(
            lead_id=lead.id,
            tenant_id=tenant_id,
            channel="email",
            recipient=lead.email,
            subject=f"{tenant.name} — ready to help with {lead.category or 'your project'}!",
            body_html="PLACEHOLDER",
            body_text="PLACEHOLDER",
            status="pending",
        )
        db.add(msg)
        db.add(LeadEvent(
            lead_id=lead.id,
            tenant_id=tenant_id,
            event_type="email_queued",
            payload={"outbound_message_id": msg.id},
        ))
        replayed += 1

    db.commit()
    log.info("Replayed %d unmapped leads for tenant %s", replayed, tenant_id)
    return {"replayed": replayed, "tenant_id": tenant_id, "tenant_name": tenant.name}
