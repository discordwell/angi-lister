"""Authentication routes — magic link login for console access."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_bypass_db
from app.models import Tenant
from app.services.auth import (
    COOKIE_NAME,
    create_magic_link,
    consume_magic_link,
    revoke_session,
    validate_session,
)
from app.services.email import send_email
from app.templates_config import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")

# Demo tenant for instant login
DEMO_TENANT_SLUG = "paschal-air"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_bypass_db)):
    """Render the login page."""
    # If already logged in, redirect to console
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and validate_session(db, cookie):
        return RedirectResponse(url="/console", status_code=302)

    # Get demo tenant info for the button
    demo_tenant = db.query(Tenant).filter(Tenant.slug == DEMO_TENANT_SLUG).first()

    return templates.TemplateResponse(request, "auth/login.html", {
        "page_title": "Sign In",
        "error": None,
        "success": None,
        "demo_tenant": demo_tenant,
    })


@router.post("/send-link", response_class=HTMLResponse)
async def send_magic_link(request: Request, db: Session = Depends(get_bypass_db)):
    """Send a magic link to the given email."""
    form = await request.form()
    email = form.get("email", "").strip().lower()

    if not email or "@" not in email:
        return templates.TemplateResponse(request, "auth/login.html", {
            "page_title": "Sign In",
            "error": "Please enter a valid email address.",
            "success": None,
            "demo_tenant": db.query(Tenant).filter(Tenant.slug == DEMO_TENANT_SLUG).first(),
        })

    link, tenant_name = create_magic_link(db, email)

    # Send the magic link email
    if settings.resend_api_key:
        try:
            from pathlib import Path
            from jinja2 import Environment, FileSystemLoader
            tpl_dir = Path(__file__).resolve().parent.parent / "templates" / "email"
            env = Environment(loader=FileSystemLoader(str(tpl_dir)), autoescape=True)
            html = env.get_template("magic_link.html").render(
                magic_link=link,
                tenant_name=tenant_name or "Netic Console",
            )
            text = f"Sign in to the Netic Console:\n\n{link}\n\nThis link expires in {settings.magic_link_ttl_minutes} minutes."
            send_email(
                recipient=email,
                subject="Sign in to Netic Console",
                body_html=html,
                body_text=text,
                sender=settings.sender_email,
            )
        except Exception as e:
            log.error("Failed to send magic link email to %s: %s", email, e)

    log.info("Magic link sent to %s: %s", email, link)

    return templates.TemplateResponse(request, "auth/login.html", {
        "page_title": "Sign In",
        "error": None,
        "success": f"Check {email} for a sign-in link.",
        "demo_tenant": db.query(Tenant).filter(Tenant.slug == DEMO_TENANT_SLUG).first(),
    })


@router.post("/demo-login")
def demo_login(db: Session = Depends(get_bypass_db)):
    """Instant login as the demo tenant (Paschal Air)."""
    demo_tenant = db.query(Tenant).filter(Tenant.slug == DEMO_TENANT_SLUG).first()
    if not demo_tenant:
        return RedirectResponse(url="/auth/login", status_code=302)

    link, _ = create_magic_link(db, demo_tenant.email)
    # Extract raw token from link
    raw_token = link.split("token=")[1]

    session = consume_magic_link(db, raw_token)
    if not session:
        return RedirectResponse(url="/auth/login", status_code=302)

    response = RedirectResponse(url="/console", status_code=302)
    response.set_cookie(
        key=COOKIE_NAME,
        value=session._cookie_value,
        httponly=True,
        samesite="lax",
        secure=not settings.app_url.startswith("http://localhost"),
        path="/",
        max_age=settings.session_ttl_days * 86400,
    )
    return response


@router.get("/callback")
def auth_callback(token: str, db: Session = Depends(get_bypass_db)):
    """Consume a magic link token and create a session."""
    session = consume_magic_link(db, token)

    if not session:
        return RedirectResponse(url="/auth/login?error=invalid", status_code=302)

    response = RedirectResponse(url="/console", status_code=302)
    response.set_cookie(
        key=COOKIE_NAME,
        value=session._cookie_value,
        httponly=True,
        samesite="lax",
        secure=not settings.app_url.startswith("http://localhost"),
        path="/",
        max_age=settings.session_ttl_days * 86400,
    )
    return response


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_bypass_db)):
    """Revoke the session and clear the cookie."""
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        revoke_session(db, cookie)

    response = RedirectResponse(url="/auth/login", status_code=302)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
