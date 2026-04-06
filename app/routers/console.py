"""Console UI routes — server-rendered HTML pages with session auth."""

import asyncio
import datetime as dt
import json
import logging
import uuid

import csv
import io

from fastapi import APIRouter, Depends, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_bypass_db, SessionLocal, set_tenant
from app.templates_config import templates
from app.models import (
    ConsoleSession, Lead, Tenant, WebhookReceipt, LeadEvent,
    TenantHomeBase, TenantJobRule, TenantSpecial, TenantFile,
)
from app.schemas.angi import AngiLeadPayload
from app.services.auth import COOKIE_NAME, validate_session
from app.services.ingestion import process_lead
from app.services.metrics import (
    get_metrics_summary,
    get_recent_leads,
    get_lead_detail,
    get_duplicate_pairs,
)
from app.services.analytics import (
    get_lead_volume_timeseries,
    get_conversion_funnel,
    get_geo_category_breakdown,
    get_duplicate_rebate_summary,
    get_conversion_detail,
    get_tenant_comparison,
    get_system_health,
    get_personalization_performance,
    get_platform_timeseries,
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
        set_tenant(auth_db, "__bypass__", session_scope=True)
        session = validate_session(auth_db, cookie)
        if session:
            # Force-load all attributes before detaching, so the object
            # stays usable after auth_db is closed.
            _ = session.tenant_id, session.email, session.id
            auth_db.expunge(session)
    finally:
        auth_db.close()

    request.state._console_session = session
    return session


def _require_session(request: Request) -> ConsoleSession:
    """Verify the user has a valid session cookie, or redirect to login."""
    session = _validate_and_cache(request)
    if not session:
        # Clear stale cookie on redirect to prevent loops
        raise HTTPException(
            status_code=302,
            headers={
                "Location": "/auth/login",
                "Set-Cookie": f"{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly",
            },
        )
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
            set_tenant(db, session.tenant_id, session_scope=True)
        else:
            set_tenant(db, "__all__", session_scope=True)
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Period parsing helper
# ---------------------------------------------------------------------------

def _parse_period(period: str) -> tuple[dt.datetime | None, dt.datetime | None, str]:
    """Parse a period string into (date_from, date_to, label).

    Supported: "all", "today", "7d", "30d", "YYYY-MM-DD" (specific day), "YYYY-MM" (specific month).
    """
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "all":
        return None, None, "All Time"
    if period == "today":
        return today, today + dt.timedelta(days=1), "Today"
    if period == "7d":
        return now - dt.timedelta(days=7), None, "Last 7 Days"
    if period == "30d":
        return now - dt.timedelta(days=30), None, "Last 30 Days"

    # Specific day: YYYY-MM-DD
    if len(period) == 10:
        try:
            day = dt.datetime.strptime(period, "%Y-%m-%d")
            return day, day + dt.timedelta(days=1), period
        except ValueError:
            pass

    # Specific month: YYYY-MM
    if len(period) == 7:
        try:
            month_start = dt.datetime.strptime(period, "%Y-%m")
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year + 1, month=1)
            else:
                month_end = month_start.replace(month=month_start.month + 1)
            return month_start, month_end, dt.datetime.strftime(month_start, "%B %Y")
        except ValueError:
            pass

    return None, None, "All Time"


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

@router.post("/leads/{lead_id}/outcome", response_class=HTMLResponse)
async def console_set_outcome(
    request: Request,
    lead_id: str,
    db: Session = Depends(get_console_db),
    session: ConsoleSession = Depends(_require_session),
):
    """Set the conversion outcome on a lead and redirect back to detail page."""
    from app.services.metrics import set_lead_outcome

    form = await request.form()
    outcome = form.get("outcome", "")
    notes = form.get("notes", "").strip() or None

    try:
        set_lead_outcome(db, lead_id, outcome, notes)
    except LookupError:
        raise HTTPException(status_code=404, detail="Lead not found")
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    db.commit()

    # Redirect back to wherever the user came from
    referer = request.headers.get("referer", "")
    if "/console/leads/" in referer:
        return RedirectResponse(url=f"/console/leads/{lead_id}", status_code=303)
    return RedirectResponse(url="/console/", status_code=303)


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

@router.get("/duplicates", response_class=HTMLResponse)
def console_duplicates(
    request: Request,
    period: str = Query("all", regex=r"^(all|today|7d|30d|\d{4}-\d{2}-\d{2}|\d{4}-\d{2})$"),
    db: Session = Depends(get_console_db),
    session: ConsoleSession = Depends(_require_session),
):
    date_from, date_to, period_label = _parse_period(period)
    pairs = get_duplicate_pairs(db, limit=500, date_from=date_from, date_to=date_to)
    return templates.TemplateResponse(request, "console/duplicates.html", {
        "pairs": pairs,
        "period": period,
        "period_label": period_label,
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
# Email Customization
# ---------------------------------------------------------------------------

def _email_context(db: Session, tenant: Tenant, session, success=None, error=None) -> dict:
    """Build the template context for the email setup page."""
    home_bases = db.query(TenantHomeBase).filter(TenantHomeBase.tenant_id == tenant.id).all()
    job_rules = db.query(TenantJobRule).filter(TenantJobRule.tenant_id == tenant.id).all()
    specials = db.query(TenantSpecial).filter(TenantSpecial.tenant_id == tenant.id).all()
    signature = db.query(TenantFile).filter(
        TenantFile.tenant_id == tenant.id, TenantFile.purpose == "signature"
    ).first()
    return {
        "page_title": "Email Setup",
        "session": session,
        "tenant": tenant,
        "home_bases": home_bases,
        "job_rules": job_rules,
        "specials": specials,
        "pricing_tiers": tenant.pricing_tiers or [],
        "signature": signature,
        "success": success,
        "error": error,
    }


def _require_tenant_session(request: Request, db: Session) -> tuple[ConsoleSession, Tenant]:
    """Require a session with a tenant_id. Returns (session, tenant)."""
    session = _require_session(request)
    if not session.tenant_id:
        raise HTTPException(status_code=403, detail="Admin accounts cannot access email settings")
    tenant = db.query(Tenant).filter(Tenant.id == session.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return session, tenant


@router.get("/email", response_class=HTMLResponse)
def console_email(
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    if not session.tenant_id:
        raise HTTPException(status_code=403, detail="Admin accounts cannot access email settings")
    tenant = db.query(Tenant).filter(Tenant.id == session.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return templates.TemplateResponse(request, "console/email.html", _email_context(db, tenant, session))


@router.post("/email", response_class=HTMLResponse)
async def console_email_save(
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    sess, tenant = _require_tenant_session(request, db)
    form = await request.form()
    action = form.get("_action", "")

    if action == "toggle":
        val = form.get("personalization_enabled", "false")
        tenant.personalization_enabled = val.lower() == "true"
        db.commit()
        status = "enabled" if tenant.personalization_enabled else "disabled"
        return templates.TemplateResponse(request, "console/email.html",
            _email_context(db, tenant, session, success=f"AI personalization {status}."))

    if action == "save_config":
        tenant.sample_email = form.get("sample_email", "").strip() or None
        tenant.llm_system_prompt = form.get("llm_system_prompt", "").strip() or None
        tenant.brand_color = form.get("brand_color", "#3b82f6").strip()
        db.commit()
        return templates.TemplateResponse(request, "console/email.html",
            _email_context(db, tenant, session, success="Voice & brand settings saved."))

    if action == "save_pricing":
        raw = form.get("pricing_raw", "").strip()
        tiers = []
        if raw:
            for line in raw.split("\n"):
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",", 1)
                if len(parts) == 2:
                    try:
                        tiers.append({"max_mi": float(parts[0].strip()), "text": parts[1].strip()})
                    except ValueError:
                        pass
        tenant.pricing_tiers = tiers if tiers else None
        db.commit()
        return templates.TemplateResponse(request, "console/email.html",
            _email_context(db, tenant, session, success=f"Pricing tiers saved ({len(tiers)} tier(s))."))

    return templates.TemplateResponse(request, "console/email.html",
        _email_context(db, tenant, session))


@router.post("/email/home-bases", response_class=HTMLResponse)
async def console_add_home_base(
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    sess, tenant = _require_tenant_session(request, db)
    form = await request.form()
    name = form.get("name", "").strip()
    try:
        lat = float(form.get("lat", ""))
        lng = float(form.get("lng", ""))
    except (ValueError, TypeError):
        return templates.TemplateResponse(request, "console/email.html",
            _email_context(db, tenant, session, error="Latitude and longitude must be numbers."))

    if not name:
        return templates.TemplateResponse(request, "console/email.html",
            _email_context(db, tenant, session, error="Location name is required."))

    db.add(TenantHomeBase(
        tenant_id=tenant.id, name=name, address=form.get("address", "").strip() or None,
        lat=lat, lng=lng,
    ))
    db.commit()
    return templates.TemplateResponse(request, "console/email.html",
        _email_context(db, tenant, session, success=f"Location '{name}' added."))


@router.post("/email/home-bases/{hb_id}/delete", response_class=HTMLResponse)
def console_delete_home_base(
    hb_id: str,
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    sess, tenant = _require_tenant_session(request, db)
    hb = db.query(TenantHomeBase).filter(
        TenantHomeBase.id == hb_id, TenantHomeBase.tenant_id == tenant.id
    ).first()
    if hb:
        db.delete(hb)
        db.commit()
    return RedirectResponse(url="/console/email", status_code=302)


@router.post("/email/job-rules", response_class=HTMLResponse)
async def console_add_job_rule(
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    sess, tenant = _require_tenant_session(request, db)
    form = await request.form()
    pattern = form.get("category_pattern", "").strip()
    rule_type = form.get("rule_type", "")

    if not pattern or rule_type not in ("whitelist", "blacklist", "wantlist"):
        return templates.TemplateResponse(request, "console/email.html",
            _email_context(db, tenant, session, error="Category pattern and valid rule type required."))

    db.add(TenantJobRule(tenant_id=tenant.id, category_pattern=pattern, rule_type=rule_type))
    db.commit()
    return templates.TemplateResponse(request, "console/email.html",
        _email_context(db, tenant, session, success=f"Rule added: {rule_type} '{pattern}'."))


@router.post("/email/job-rules/{rule_id}/delete", response_class=HTMLResponse)
def console_delete_job_rule(
    rule_id: str,
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    sess, tenant = _require_tenant_session(request, db)
    rule = db.query(TenantJobRule).filter(
        TenantJobRule.id == rule_id, TenantJobRule.tenant_id == tenant.id
    ).first()
    if rule:
        db.delete(rule)
        db.commit()
    return RedirectResponse(url="/console/email", status_code=302)


@router.post("/email/specials", response_class=HTMLResponse)
async def console_add_special(
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    sess, tenant = _require_tenant_session(request, db)
    form = await request.form()
    name = form.get("name", "").strip()
    discount_text = form.get("discount_text", "").strip()

    if not name or not discount_text:
        return templates.TemplateResponse(request, "console/email.html",
            _email_context(db, tenant, session, error="Name and discount text are required."))

    conditions = {}
    if form.get("cond_category", "").strip():
        conditions["category_contains"] = form.get("cond_category").strip()
    if form.get("cond_max_distance", "").strip():
        try:
            conditions["max_distance_mi"] = float(form.get("cond_max_distance"))
        except ValueError:
            pass
    if form.get("cond_valid_before", "").strip():
        conditions["valid_before"] = form.get("cond_valid_before").strip()
    if form.get("cond_urgency", "").strip():
        conditions["urgency_in"] = [form.get("cond_urgency").strip()]

    db.add(TenantSpecial(
        tenant_id=tenant.id, name=name, discount_text=discount_text,
        description=form.get("description", "").strip() or None,
        conditions=conditions,
    ))
    db.commit()
    return templates.TemplateResponse(request, "console/email.html",
        _email_context(db, tenant, session, success=f"Special '{name}' added."))


@router.post("/email/specials/{special_id}/delete", response_class=HTMLResponse)
def console_delete_special(
    special_id: str,
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    sess, tenant = _require_tenant_session(request, db)
    sp = db.query(TenantSpecial).filter(
        TenantSpecial.id == special_id, TenantSpecial.tenant_id == tenant.id
    ).first()
    if sp:
        db.delete(sp)
        db.commit()
    return RedirectResponse(url="/console/email", status_code=302)


MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}


@router.post("/email/signature", response_class=HTMLResponse)
async def console_upload_signature(
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    sess, tenant = _require_tenant_session(request, db)
    form = await request.form()
    upload = form.get("file")

    if not upload or not hasattr(upload, "read"):
        return templates.TemplateResponse(request, "console/email.html",
            _email_context(db, tenant, session, error="Please select a file to upload."))

    content_type = upload.content_type or ""
    if content_type not in ALLOWED_CONTENT_TYPES:
        return templates.TemplateResponse(request, "console/email.html",
            _email_context(db, tenant, session, error=f"Unsupported file type: {content_type}. Use PNG, JPEG, GIF, WebP, or SVG."))

    data = await upload.read()
    if len(data) > MAX_UPLOAD_BYTES:
        return templates.TemplateResponse(request, "console/email.html",
            _email_context(db, tenant, session, error="File too large. Maximum 2 MB."))

    # Replace any existing signature
    existing = db.query(TenantFile).filter(
        TenantFile.tenant_id == tenant.id, TenantFile.purpose == "signature"
    ).first()
    if existing:
        db.delete(existing)

    db.add(TenantFile(
        tenant_id=tenant.id,
        filename=upload.filename or "signature",
        content_type=content_type,
        size_bytes=len(data),
        data=data,
        purpose="signature",
    ))
    db.commit()

    return templates.TemplateResponse(request, "console/email.html",
        _email_context(db, tenant, session, success="Signature uploaded."))


@router.post("/email/signature/delete", response_class=HTMLResponse)
def console_delete_signature(
    request: Request,
    db: Session = Depends(get_bypass_db),
    session: ConsoleSession = Depends(_require_session),
):
    sess, tenant = _require_tenant_session(request, db)
    existing = db.query(TenantFile).filter(
        TenantFile.tenant_id == tenant.id, TenantFile.purpose == "signature"
    ).first()
    if existing:
        db.delete(existing)
        db.commit()
    return RedirectResponse(url="/console/email", status_code=302)


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
                    set_tenant(db, tenant_id, session_scope=True)
                else:
                    set_tenant(db, "__all__", session_scope=True)

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


# ---------------------------------------------------------------------------
# Analytics — client (tenant-scoped via RLS)
# ---------------------------------------------------------------------------

@router.get("/analytics", response_class=HTMLResponse)
def console_analytics(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_console_db),
    session: ConsoleSession = Depends(_require_session),
):
    volume = get_lead_volume_timeseries(db, days=days)
    funnel = get_conversion_funnel(db, days=days)
    geo_cat = get_geo_category_breakdown(db, days=days)
    rebate = get_duplicate_rebate_summary(db, days=days)
    conversion = get_conversion_detail(db, days=days)

    return templates.TemplateResponse(request, "console/analytics.html", {
        "volume": volume,
        "funnel": funnel,
        "geo_cat": geo_cat,
        "rebate": rebate,
        "conversion": conversion,
        "days": days,
        "page_title": "Analytics",
        "session": session,
    })


# ---------------------------------------------------------------------------
# Analytics — admin (cross-tenant, 403 if not admin)
# ---------------------------------------------------------------------------

@router.get("/analytics/admin", response_class=HTMLResponse)
def console_analytics_admin(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_console_db),
    session: ConsoleSession = Depends(_require_session),
):
    if session.tenant_id is not None:
        raise HTTPException(status_code=403, detail="Admin access required")

    comparison = get_tenant_comparison(db, days=days)
    health = get_system_health(db)
    personalization = get_personalization_performance(db, days=days)
    timeseries = get_platform_timeseries(db, days=days)

    return templates.TemplateResponse(request, "console/analytics_admin.html", {
        "comparison": comparison,
        "health": health,
        "personalization": personalization,
        "timeseries": timeseries,
        "days": days,
        "page_title": "Admin Analytics",
        "session": session,
    })


# ---------------------------------------------------------------------------
# Analytics — duplicate CSV export (tenant-scoped)
# ---------------------------------------------------------------------------

@router.get("/duplicates/export")
def console_duplicates_export(
    request: Request,
    period: str = Query("all"),
    db: Session = Depends(get_console_db),
    session: ConsoleSession = Depends(_require_session),
):
    date_from, date_to, period_label = _parse_period(period)
    rows = get_duplicate_pairs(db, limit=10000, date_from=date_from, date_to=date_to)

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
