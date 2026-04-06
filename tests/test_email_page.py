"""Tests for the email customization console page."""

import pytest
from fastapi.testclient import TestClient

from app.models import (
    ConsoleSession, Tenant, AngiMapping,
    TenantHomeBase, TenantJobRule, TenantSpecial,
)
from app.services.auth import _hash


@pytest.fixture
def tenant_session(db):
    """Create a tenant with a console session. Returns (tenant, session, cookie_value)."""
    t = Tenant(
        name="Email Test Co", slug="email-test",
        email="email@test.example.com", phone="5550001111",
        brand_color="#ff6600",
    )
    db.add(t)
    db.flush()

    # Create a session directly (bypass magic link)
    import datetime as dt
    import secrets
    raw_token = "sess_" + secrets.token_urlsafe(32)
    session = ConsoleSession(
        tenant_id=t.id,
        email="email@test.example.com",
        session_token_hash=_hash(raw_token),
        expires_at=dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(days=7),
    )
    db.add(session)
    db.flush()

    # Build a signed cookie
    from app.services.auth import _sign_cookie
    cookie_value = _sign_cookie({
        "token": raw_token,
        "email": session.email,
        "tenant_id": t.id,
        "exp": int(session.expires_at.timestamp() * 1000),
    })
    return t, session, cookie_value


@pytest.fixture
def email_client(tenant_session, db):
    """TestClient authenticated as the tenant."""
    from app.main import create_app
    from app.db.session import get_db, get_bypass_db
    import app.routers.console as console_mod

    app = create_app()

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    app.dependency_overrides[get_bypass_db] = override

    # Patch SessionLocal so _validate_and_cache uses the test DB
    original_sl = console_mod.SessionLocal
    console_mod.SessionLocal = lambda: db

    tenant, session, cookie = tenant_session

    with TestClient(app, cookies={"angi_session": cookie}) as c:
        yield c, tenant

    console_mod.SessionLocal = original_sl


class TestEmailPageLoad:
    def test_get_email_page(self, email_client):
        client, tenant = email_client
        resp = client.get("/console/email")
        assert resp.status_code == 200
        assert "Email Setup" in resp.text
        assert "AI Personalization" in resp.text

    def test_admin_cannot_access(self, db):
        """Admin sessions (no tenant_id) should get 403."""
        import datetime as dt
        import secrets
        from app.main import create_app
        from app.db.session import get_db, get_bypass_db
        from app.services.auth import _sign_cookie
        import app.routers.console as console_mod

        raw_token = "sess_" + secrets.token_urlsafe(32)
        session = ConsoleSession(
            tenant_id=None,
            email="admin@netic.ai",
            session_token_hash=_hash(raw_token),
            expires_at=dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(days=7),
        )
        db.add(session)
        db.flush()

        cookie = _sign_cookie({
            "token": raw_token, "email": session.email,
            "tenant_id": None,
            "exp": int(session.expires_at.timestamp() * 1000),
        })

        app = create_app()

        def override():
            yield db

        app.dependency_overrides[get_db] = override
        app.dependency_overrides[get_bypass_db] = override

        original_sl = console_mod.SessionLocal
        console_mod.SessionLocal = lambda: db

        with TestClient(app, cookies={"angi_session": cookie}) as c:
            resp = c.get("/console/email")
            assert resp.status_code == 403

        console_mod.SessionLocal = original_sl


class TestTogglePersonalization:
    def test_enable_personalization(self, email_client):
        client, tenant = email_client

        resp = client.post("/console/email", data={
            "_action": "toggle",
            "personalization_enabled": "true",
        })
        assert resp.status_code == 200
        assert "enabled" in resp.text.lower()
        # Verify the page now shows "Active" badge
        assert "Active" in resp.text


class TestVoiceAndBrand:
    def test_save_sample_email(self, email_client):
        client, _ = email_client
        resp = client.post("/console/email", data={
            "_action": "save_config",
            "sample_email": "We love helping customers!",
            "llm_system_prompt": "Always mention our warranty.",
            "brand_color": "#00ff00",
        })
        assert resp.status_code == 200
        assert "saved" in resp.text.lower()
        # Verify values persisted by checking they appear in the form
        assert "We love helping customers!" in resp.text
        assert "Always mention our warranty." in resp.text
        assert "#00ff00" in resp.text


class TestPricingTiers:
    def test_save_pricing(self, email_client):
        client, _ = email_client
        resp = client.post("/console/email", data={
            "_action": "save_pricing",
            "pricing_raw": "1, $39 diagnostic\n5, $59 diagnostic",
        })
        assert resp.status_code == 200
        assert "2 tier(s)" in resp.text
        assert "$39 diagnostic" in resp.text


class TestHomeBaseCRUD:
    def test_add_and_delete(self, email_client):
        client, _ = email_client

        resp = client.post("/console/email/home-bases", data={
            "name": "Test Shop", "address": "123 Main", "lat": "39.77", "lng": "-86.15",
        })
        assert resp.status_code == 200
        assert "Test Shop" in resp.text

        # Extract the home base ID from the delete form in the HTML
        import re
        match = re.search(r'/console/email/home-bases/([a-f0-9-]+)/delete', resp.text)
        assert match, "Should have a delete form with the home base ID"
        hb_id = match.group(1)

        resp = client.post(f"/console/email/home-bases/{hb_id}/delete")
        assert resp.status_code == 200

    def test_invalid_coords(self, email_client):
        client, _ = email_client
        resp = client.post("/console/email/home-bases", data={
            "name": "Bad", "lat": "abc", "lng": "def",
        })
        assert resp.status_code == 200
        assert "must be numbers" in resp.text.lower()


class TestJobRuleCRUD:
    def test_add_and_delete(self, email_client):
        client, _ = email_client

        resp = client.post("/console/email/job-rules", data={
            "category_pattern": "HVAC", "rule_type": "whitelist",
        })
        assert resp.status_code == 200
        assert "HVAC" in resp.text

        import re
        match = re.search(r'/console/email/job-rules/([a-f0-9-]+)/delete', resp.text)
        assert match
        rule_id = match.group(1)

        resp = client.post(f"/console/email/job-rules/{rule_id}/delete")
        assert resp.status_code == 200


class TestSpecialCRUD:
    def test_add_and_delete(self, email_client):
        client, _ = email_client

        resp = client.post("/console/email/specials", data={
            "name": "Spring Deal", "discount_text": "$49 tune-up",
            "cond_category": "AC",
        })
        assert resp.status_code == 200
        assert "Spring Deal" in resp.text
        assert "$49 tune-up" in resp.text

        import re
        match = re.search(r'/console/email/specials/([a-f0-9-]+)/delete', resp.text)
        assert match
        sp_id = match.group(1)

        resp = client.post(f"/console/email/specials/{sp_id}/delete")
        assert resp.status_code == 200
