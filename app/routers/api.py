"""JSON API endpoints for the console and external integrations."""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.api import MetricsSummary, LeadSummary, LeadDetail, DuplicatePair
from app.services.metrics import (
    get_metrics_summary,
    get_recent_leads,
    get_lead_detail,
    get_duplicate_pairs,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


@router.get("/metrics", response_model=MetricsSummary)
def api_metrics(db: Session = Depends(get_db)):
    """Return current KPI metrics."""
    data = get_metrics_summary(db)
    return MetricsSummary(**data)


@router.get("/leads", response_model=list[LeadSummary])
def api_leads(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Return recent leads (most recent first)."""
    rows = get_recent_leads(db, limit=limit)
    return [LeadSummary(**r) for r in rows]


@router.get("/leads/{lead_id}", response_model=LeadDetail)
def api_lead_detail(lead_id: str, db: Session = Depends(get_db)):
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
    db: Session = Depends(get_db),
):
    """Return duplicate match pairs."""
    rows = get_duplicate_pairs(db, limit=limit)
    return [DuplicatePair(**r) for r in rows]
