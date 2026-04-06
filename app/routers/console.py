"""Console UI routes — server-rendered HTML pages with session auth."""

import asyncio
import datetime as dt
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_bypass_db, SessionLocal, set_tenant
from app.templates_config import templates
from app.models import ConsoleSession, Lead, Tenant, WebhookReceipt, LeadEvent
from app.schemas.angi import AngiLeadPayload
from app.services.auth import COOKIE_NAME, validate_session
from app.services.ingestion import process_lead
from app.services.metrics import (
    get_metrics_summary,
    get_recent_leads,
    get_lead_detail,
    get_duplicate_pairs,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/console")


# ---------------------------------------------------------------------------
# Auth helpers — validate once per request, cache on request.state
# ---------------------------------------------------------------------------

def _validate_and_cache(request: Request) -> ConsoleSession | None:
    """Validate session once per request using a transient DB session."""
    if hasattr(request.state, "_console_session"):
        return request.state._console_session

    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        request.state._console_session = None
        return None

    # Use a separate transient session for auth validation so the main
    # tenant-scoped session isn't affected by validate_session's commit.
    auth_db = SessionLocal()
    try:
        set_tenant(auth_db, "__bypass__")
        session = validate_session(auth_db, cookie)
    finally:
        auth_db.close()

    request.state._console_session = session
    return session


def _require_session(request: Request) -> ConsoleSession:
    """Verify the user has a valid session cookie, or redirect to login."""
    session = _validate_and_cache(request)
    if not session:
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})
    return session


def get_console_db(request: Request):
    """Tenant-scoped DB for console routes.

    - If session.tenant_id is set: scope to that tenant (RLS enforced)
    - If session.tenant_id is None: admin mode (__all__)
    """
    session = _validate_and_cache(request)
    db = SessionLocal()
    try:
        if session and session.tenant_id:
            set_tenant(db, session.tenant_id)
        else:
            set_tenant(db, "__all__")
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def console_dashboard(
    request: Request,
    db: Session = Depends(get_console_db),
    session: ConsoleSession = Depends(_require_session),
):
    metrics = get_metrics_summary(db)
    leads = get_recent_leads(db, limit=50)
    return templates.TemplateResponse(request, "console/dashboard.html", {
        "metrics": metrics,
        "leads": leads,
        "page_title": "Dashboard",
        "session": session,
    })


# ---------------------------------------------------------------------------
# Lead detail
# ---------------------------------------------------------------------------

@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def console_lead_detail(
    request: Request,
    lead_id: str,
    db: Session = Depends(get_console_db),
    session: ConsoleSession = Depends(_require_session),
):
    detail = get_lead_detail(db, lead_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Lead not found")
    return templates.TemplateResponse(request, "console/lead_detail.html", {
        "lead": detail,
        "page_title": f"Lead: {detail['first_name']} {detail['last_name']}",
        "session": session,
    })


# ---------------------------------------------------------------------------
# Lead outcome
# ---------------------------------------------------------------------------

VALID_OUTCOMES = {"booked", "won", "lost"}
OUTCOME_VALID_FROM = {"mapped", "booked", "won", "lost"}


@router.post("/leads/{lead_id}/outcome", response_class=HTMLResponse)
async def console_set_outcome(
    request: Request,
    lead_id: str,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    """Set the conversion outcome on a lead and redirect back to detail page."""
    form = await request.form()
    outcome = form.get("outcome", "")
    notes = form.get("notes", "").strip() or None

    if outcome not in VALID_OUTCOMES:
        raise HTTPException(status_code=422, detail=f"Invalid outcome: {outcome}")

    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    if lead.status not in OUTCOME_VALID_FROM:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot set outcome on lead with status '{lead.status}'",
        )

    previous_status = lead.status
    lead.status = outcome

    db.add(LeadEvent(
        lead_id=lead.id,
        tenant_id=lead.tenant_id,
        event_type=f"outcome_{outcome}",
        payload={"notes": notes, "previous_status": previous_status},
    ))
    db.commit()

    log.info("Lead %s outcome set to '%s' via console", lead.id, outcome)
    return RedirectResponse(url=f"/console/leads/{lead_id}", status_code=303)


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

@router.get("/duplicates", response_class=HTMLResponse)
def console_duplicates(
    request: Request,
    db: Session = Depends(get_console_db),
    session: ConsoleSession = Depends(_require_session),
):
    pairs = get_duplicate_pairs(db, limit=100)
    return templates.TemplateResponse(request, "console/duplicates.html", {
        "pairs": pairs,
        "page_title": "Duplicate Leads",
        "session": session,
    })


# ---------------------------------------------------------------------------
# Simulate lead
# ---------------------------------------------------------------------------

@router.get("/simulate", response_class=HTMLResponse)
def console_simulate_form(
    request: Request,
    session: ConsoleSession = Depends(_require_session),
):
    return templates.TemplateResponse(request, "console/simulate.html", {
        "page_title": "Simulate Lead",
        "result": None,
        "error": None,
        "form_data": None,
        "session": session,
    })


@router.post("/simulate", response_class=HTMLResponse)
async def console_simulate_submit(
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    """Process a simulated lead submission from the console form.

    Uses bypass DB because ingestion inserts across multiple RLS-protected tables.
    """
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
            "session": session,
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
        "session": session,
    })


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
def console_settings(
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    is_admin = session.tenant_id is None
    tenant = db.query(Tenant).filter(Tenant.id == session.tenant_id).first() if session.tenant_id else None

    return templates.TemplateResponse(request, "console/settings.html", {
        "page_title": "Settings",
        "session": session,
        "is_admin": is_admin,
        "tenant": tenant,
        "current_email": session.email,
        "current_name": tenant.name if tenant else "Admin",
        "success": None,
        "error": None,
    })


@router.post("/settings", response_class=HTMLResponse)
async def console_settings_save(
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    form = await request.form()
    new_email = form.get("email", "").strip()
    new_name = form.get("display_name", "").strip()

    is_admin = session.tenant_id is None
    tenant = db.query(Tenant).filter(Tenant.id == session.tenant_id).first() if session.tenant_id else None

    if not new_email or "@" not in new_email:
        return templates.TemplateResponse(request, "console/settings.html", {
            "page_title": "Settings",
            "session": session,
            "is_admin": is_admin,
            "tenant": tenant,
            "current_email": session.email,
            "current_name": tenant.name if tenant else "Admin",
            "success": None,
            "error": "Please enter a valid email address.",
        })

    # Update session email
    session.email = new_email
    db.add(session)

    # Update tenant name + email if tenant user
    if tenant and not is_admin:
        if new_name:
            tenant.name = new_name
            tenant.email_from_name = new_name
        tenant.email = new_email
        db.add(tenant)

    db.commit()

    log.info("Settings updated for session %s (email=%s)", session.id, new_email)

    return templates.TemplateResponse(request, "console/settings.html", {
        "page_title": "Settings",
        "session": session,
        "is_admin": is_admin,
        "tenant": tenant,
        "current_email": new_email,
        "current_name": tenant.name if tenant else "Admin",
        "success": "Settings saved.",
        "error": None,
    })


# ---------------------------------------------------------------------------
# SSE — real-time event stream
# ---------------------------------------------------------------------------

@router.get("/events")
async def console_events(
    request: Request,
    session: ConsoleSession = Depends(_require_session),
):
    """Server-Sent Events endpoint for real-time lead event updates."""
    tenant_id = session.tenant_id

    async def event_generator():
        last_seen = dt.datetime.now(dt.UTC)
        while True:
            if await request.is_disconnected():
                break
            db = SessionLocal()
            try:
                if tenant_id:
                    set_tenant(db, tenant_id)
                else:
                    set_tenant(db, "__all__")

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
