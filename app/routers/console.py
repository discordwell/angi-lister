"""Console UI routes — server-rendered HTML pages with HTTP Basic auth."""

import asyncio
import datetime as dt
import json
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db, SessionLocal
from app.templates_config import templates
from app.models import WebhookReceipt, LeadEvent
from app.schemas.angi import AngiLeadPayload
from app.services.ingestion import process_lead
from app.services.metrics import (
    get_metrics_summary,
    get_recent_leads,
    get_lead_detail,
    get_duplicate_pairs,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/console")
security = HTTPBasic()


def _verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify HTTP Basic credentials against settings."""
    correct_user = secrets.compare_digest(credentials.username, settings.console_user)
    correct_pass = secrets.compare_digest(credentials.password, settings.console_password)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def console_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    credentials: HTTPBasicCredentials = Depends(_verify_credentials),
):
    metrics = get_metrics_summary(db)
    leads = get_recent_leads(db, limit=50)
    return templates.TemplateResponse(request, "console/dashboard.html", {
        "metrics": metrics,
        "leads": leads,
        "page_title": "Dashboard",
    })


# ---------------------------------------------------------------------------
# Lead detail
# ---------------------------------------------------------------------------

@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def console_lead_detail(
    request: Request,
    lead_id: str,
    db: Session = Depends(get_db),
    credentials: HTTPBasicCredentials = Depends(_verify_credentials),
):
    detail = get_lead_detail(db, lead_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Lead not found")
    return templates.TemplateResponse(request, "console/lead_detail.html", {
        "lead": detail,
        "page_title": f"Lead: {detail['first_name']} {detail['last_name']}",
    })


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

@router.get("/duplicates", response_class=HTMLResponse)
def console_duplicates(
    request: Request,
    db: Session = Depends(get_db),
    credentials: HTTPBasicCredentials = Depends(_verify_credentials),
):
    pairs = get_duplicate_pairs(db, limit=100)
    return templates.TemplateResponse(request, "console/duplicates.html", {
        "pairs": pairs,
        "page_title": "Duplicate Leads",
    })


# ---------------------------------------------------------------------------
# Simulate lead
# ---------------------------------------------------------------------------

@router.get("/simulate", response_class=HTMLResponse)
def console_simulate_form(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(_verify_credentials),
):
    return templates.TemplateResponse(request, "console/simulate.html", {
        "page_title": "Simulate Lead",
        "result": None,
        "error": None,
        "form_data": None,
    })


@router.post("/simulate", response_class=HTMLResponse)
async def console_simulate_submit(
    request: Request,
    db: Session = Depends(get_db),
    credentials: HTTPBasicCredentials = Depends(_verify_credentials),
):
    """Process a simulated lead submission from the console form."""
    form = await request.form()

    form_data = {
        "FirstName": form.get("first_name", ""),
        "LastName": form.get("last_name", ""),
        "PhoneNumber": form.get("phone", ""),
        "Email": form.get("email", ""),
        "Source": form.get("source", "Console Simulation"),
        "Description": form.get("description", ""),
        "Category": form.get("category", ""),
        "Urgency": form.get("urgency", ""),
        "CorrelationId": form.get("correlation_id", "") or str(uuid.uuid4()),
        "ALAccountId": form.get("al_account_id", ""),
        "PostalAddress": {
            "AddressFirstLine": form.get("address_line1", ""),
            "AddressSecondLine": form.get("address_line2", ""),
            "City": form.get("city", ""),
            "State": form.get("state", ""),
            "PostalCode": form.get("postal_code", ""),
        },
    }

    try:
        payload = AngiLeadPayload.model_validate(form_data)
    except ValidationError as exc:
        return templates.TemplateResponse(request, "console/simulate.html", {
            "page_title": "Simulate Lead",
            "result": None,
            "error": f"Validation error: {exc.error_count()} issue(s). {exc.errors()}",
            "form_data": form_data,
        })

    # Create a synthetic webhook receipt
    receipt = WebhookReceipt(
        headers={"x-source": "console-simulation"},
        raw_body=form_data,
        auth_valid=True,
        correlation_id=form_data["CorrelationId"],
    )
    db.add(receipt)
    db.flush()

    lead = process_lead(db, receipt, payload, is_simulated=True)
    db.commit()

    log.info("Simulated lead created via console: %s", lead.id)

    return templates.TemplateResponse(request, "console/simulate.html", {
        "page_title": "Simulate Lead",
        "result": {
            "lead_id": lead.id,
            "correlation_id": lead.correlation_id,
            "status": lead.status,
        },
        "error": None,
        "form_data": form_data,
    })


# ---------------------------------------------------------------------------
# SSE — real-time event stream
# ---------------------------------------------------------------------------

@router.get("/events")
async def console_events(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(_verify_credentials),
):
    """Server-Sent Events endpoint for real-time lead event updates."""

    async def event_generator():
        last_seen = dt.datetime.now(dt.UTC)
        while True:
            if await request.is_disconnected():
                break
            db = SessionLocal()
            try:
                events = (
                    db.query(LeadEvent)
                    .filter(LeadEvent.created_at > last_seen)
                    .order_by(LeadEvent.created_at.asc())
                    .limit(50)
                    .all()
                )
                for event in events:
                    data = json.dumps({
                        "id": event.id,
                        "event_type": event.event_type,
                        "lead_id": event.lead_id,
                        "payload": event.payload,
                        "created_at": str(event.created_at),
                    })
                    yield f"data: {data}\n\n"
                    last_seen = event.created_at
            finally:
                db.close()
            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
