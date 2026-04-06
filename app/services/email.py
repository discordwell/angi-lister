"""Email rendering and sending service.

Uses Jinja2 for template rendering and Resend (via httpx) for delivery.
When resend_api_key is empty, emails are marked as simulated and logged
instead of sent.
"""

import datetime as dt
import logging
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Lead, OutboundMessage, Tenant, LeadEvent

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"
_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=True,
)

RESEND_API_URL = "https://api.resend.com/emails"
MAX_ATTEMPTS = 3


def send_email(recipient: str, subject: str, body_html: str, body_text: str,
               sender: str | None = None) -> str | None:
    """Send an email via Resend. Returns provider ID or None."""
    if not settings.resend_api_key:
        log.info("Resend not configured — skipping email to %s", recipient)
        return None
    resp = httpx.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": sender or settings.sender_email,
            "to": [recipient],
            "subject": subject,
            "html": body_html,
            "text": body_text,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json().get("id")


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

def _template_context(lead: Lead, tenant: Tenant) -> dict:
    """Build the Jinja2 context dict shared by HTML and text templates."""
    custom_body = None
    if tenant.intro_template:
        # Render the tenant's custom body snippet (stored as a Jinja2 string)
        try:
            custom_tpl = _jinja_env.from_string(tenant.intro_template)
            custom_body = custom_tpl.render(
                first_name=lead.first_name,
                last_name=lead.last_name,
                category=lead.category,
                description=lead.description,
            )
        except Exception:
            log.exception("Failed to render tenant intro_template for tenant %s", tenant.id)

    return {
        "tenant_name": tenant.name,
        "brand_color": tenant.brand_color or "#2563eb",
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "category": lead.category,
        "description": lead.description,
        "tenant_phone": tenant.phone,
        "custom_body": custom_body,
        "year": dt.datetime.now(dt.UTC).year,
    }


def render_intro_email(lead: Lead, tenant: Tenant) -> tuple[str, str]:
    """Render the intro email and return (html, text)."""
    ctx = _template_context(lead, tenant)
    html = _jinja_env.get_template("intro.html").render(**ctx)
    text = _jinja_env.get_template("intro.txt").render(**ctx)
    return html, text


# ---------------------------------------------------------------------------
# Populating outbound messages with rendered content
# ---------------------------------------------------------------------------

def populate_outbound(db: Session, msg: OutboundMessage) -> None:
    """Fill body_html / body_text on a pending OutboundMessage if still PLACEHOLDER."""
    if msg.body_html != "PLACEHOLDER":
        return  # already rendered

    lead = msg.lead
    if not lead or not lead.tenant:
        log.warning("Cannot render msg %s — missing lead or tenant", msg.id)
        return

    tenant = lead.tenant
    html, text = render_intro_email(lead, tenant)
    msg.body_html = html
    msg.body_text = text
    db.flush()
    log.info("Rendered email body for outbound message %s", msg.id)


# ---------------------------------------------------------------------------
# Sending via Resend
# ---------------------------------------------------------------------------

def send_outbound_message(db: Session, msg: OutboundMessage) -> bool:
    """Attempt to send an outbound message via Resend.

    Returns True on success, False on failure.  Updates the message row
    with status / attempts / error info regardless.
    """
    msg.attempts += 1

    # Simulated sends — log and mark sent without hitting the API
    if msg.is_simulated or not settings.resend_api_key:
        msg.status = "sent"
        msg.sent_at = dt.datetime.now(dt.UTC)
        msg.provider_id = "simulated"
        db.flush()
        log.info("Simulated send for message %s to %s", msg.id, msg.recipient)
        return True

    # Real send via Resend REST API
    try:
        resp = httpx.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": settings.sender_email,
                "to": [msg.recipient],
                "subject": msg.subject,
                "html": msg.body_html,
                "text": msg.body_text,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        msg.status = "sent"
        msg.sent_at = dt.datetime.now(dt.UTC)
        msg.provider_id = data.get("id", "unknown")
        db.flush()
        log.info("Sent message %s via Resend (provider_id=%s)", msg.id, msg.provider_id)
        return True

    except httpx.HTTPStatusError as exc:
        error_body = exc.response.text[:500]
        msg.last_error = f"HTTP {exc.response.status_code}: {error_body}"
        log.error("Resend HTTP error for msg %s: %s", msg.id, msg.last_error)

    except httpx.RequestError as exc:
        msg.last_error = f"Request error: {exc!r}"[:500]
        log.error("Resend request error for msg %s: %s", msg.id, msg.last_error)

    except Exception as exc:
        msg.last_error = f"Unexpected: {exc!r}"[:500]
        log.exception("Unexpected error sending msg %s", msg.id)

    # Mark as failed if we've exhausted attempts
    if msg.attempts >= MAX_ATTEMPTS:
        msg.status = "failed"
        log.warning("Message %s failed after %d attempts", msg.id, msg.attempts)
    db.flush()
    return False


# ---------------------------------------------------------------------------
# Process a single outbound message end-to-end
# ---------------------------------------------------------------------------

def process_outbound_message(db: Session, msg: OutboundMessage) -> bool:
    """Render (if needed) and send a single outbound message.

    Creates a LeadEvent on success or final failure.
    Returns True if the message was sent successfully.
    """
    # Step 1: Personalize or render template into body fields
    if msg.body_html == "PLACEHOLDER":
        lead = msg.lead
        tenant = lead.tenant if lead else None

        if tenant and tenant.personalization_enabled:
            try:
                from app.services.personalization import personalize_outbound

                should_send = personalize_outbound(db, msg)
                if not should_send:
                    return False
            except Exception:
                log.exception(
                    "Personalization failed for msg %s — falling back to Jinja2", msg.id
                )
                msg.generation_method = "jinja2_fallback"
                populate_outbound(db, msg)
        else:
            msg.generation_method = "jinja2"
            populate_outbound(db, msg)

    # Step 2: Send
    success = send_outbound_message(db, msg)

    # Step 3: Record event
    if success:
        db.add(LeadEvent(
            lead_id=msg.lead_id,
            tenant_id=msg.tenant_id,
            event_type="email_sent",
            payload={
                "outbound_message_id": msg.id,
                "provider_id": msg.provider_id,
                "is_simulated": msg.is_simulated or not settings.resend_api_key,
            },
        ))
    elif msg.status == "failed":
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

    db.flush()
    return success
