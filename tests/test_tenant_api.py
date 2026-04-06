"""Tests for the tenant-facing API."""

import uuid

import pytest

from app.models import ApiKey, Lead, Tenant, AngiMapping
from app.services.api_auth import generate_api_key


@pytest.fixture
def tenant_with_key(db):
    """Create a tenant and generate an API key. Returns (tenant, raw_key)."""
    t = Tenant(
        name="Test Tenant", slug="test-tenant-api",
        email="test@tenant.example.com", phone="5550000000",
        brand_color="#123456", timezone="America/Chicago",
    )
    db.add(t)
    db.flush()
    db.add(AngiMapping(al_account_id="900001", tenant_id=t.id))
    _, raw_key = generate_api_key(db, tenant_id=t.id, name="test key")
    db.flush()
    return t, raw_key


@pytest.fixture
def tenant_client(tenant_with_key, db):
    """TestClient with a valid tenant API key."""
    from app.main import create_app
    from app.db.session import get_db, get_bypass_db

    app = create_app()

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    app.dependency_overrides[get_bypass_db] = override

    # Also override SessionLocal in api_auth so tenant-scoped sessions use test DB
    import app.services.api_auth as auth_mod
    original_session_local = auth_mod.SessionLocal
    auth_mod.SessionLocal = lambda: db

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c, tenant_with_key[0], tenant_with_key[1]

    auth_mod.SessionLocal = original_session_local


def _headers(raw_key: str) -> dict:
    return {"Authorization": f"Bearer {raw_key}"}


class TestTenantAuth:
    def test_no_auth_returns_401(self, tenant_client):
        client, _, _ = tenant_client
        resp = client.get("/api/v1/tenant/me")
        assert resp.status_code == 401

    def test_bad_key_returns_401(self, tenant_client):
        client, _, _ = tenant_client
        resp = client.get("/api/v1/tenant/me", headers=_headers("angi_bad_key"))
        assert resp.status_code == 401

    def test_valid_key_returns_profile(self, tenant_client):
        client, tenant, key = tenant_client
        resp = client.get("/api/v1/tenant/me", headers=_headers(key))
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Tenant"
        assert data["slug"] == "test-tenant-api"


class TestTenantLeads:
    def test_leads_scoped_to_tenant(self, tenant_client, db):
        client, tenant, key = tenant_client

        # Create leads for our tenant and another
        other = Tenant(name="Other", slug="other-t", email="o@o.com")
        db.add(other)
        db.flush()

        db.add(Lead(
            correlation_id=str(uuid.uuid4()), tenant_id=tenant.id,
            al_account_id="900001", status="mapped",
            first_name="Mine", last_name="Lead", email="m@e.com", phone="111",
            raw_payload={}, fingerprint="x",
        ))
        db.add(Lead(
            correlation_id=str(uuid.uuid4()), tenant_id=other.id,
            al_account_id="900099", status="mapped",
            first_name="Other", last_name="Lead", email="o@e.com", phone="222",
            raw_payload={}, fingerprint="y",
        ))
        db.flush()

        resp = client.get("/api/v1/tenant/leads", headers=_headers(key))
        assert resp.status_code == 200
        leads = resp.json()
        assert all(l["tenant_name"] == "Test Tenant" for l in leads)

    def test_lead_detail_wrong_tenant_404(self, tenant_client, db):
        client, tenant, key = tenant_client

        other = Tenant(name="Other2", slug="other-t2", email="o2@o.com")
        db.add(other)
        db.flush()
        other_lead = Lead(
            correlation_id=str(uuid.uuid4()), tenant_id=other.id,
            al_account_id="900098", status="mapped",
            first_name="X", last_name="Y", email="x@y.com", phone="333",
            raw_payload={}, fingerprint="z",
        )
        db.add(other_lead)
        db.flush()

        resp = client.get(f"/api/v1/tenant/leads/{other_lead.id}", headers=_headers(key))
        assert resp.status_code == 404


class TestTenantMetrics:
    def test_metrics_returns_200(self, tenant_client):
        client, _, key = tenant_client
        resp = client.get("/api/v1/tenant/metrics", headers=_headers(key))
        assert resp.status_code == 200
        data = resp.json()
        assert "total_leads_24h" in data


class TestTenantConfig:
    def test_get_config(self, tenant_client):
        client, _, key = tenant_client
        resp = client.get("/api/v1/tenant/config", headers=_headers(key))
        assert resp.status_code == 200
        data = resp.json()
        assert "home_bases" in data
        assert "job_rules" in data
        assert "specials" in data

    def test_update_config(self, tenant_client):
        client, _, key = tenant_client
        resp = client.put(
            "/api/v1/tenant/config",
            json={"personalization_enabled": True, "sample_email": "Hello!"},
            headers=_headers(key),
        )
        assert resp.status_code == 200
        assert resp.json()["personalization_enabled"] is True
        assert resp.json()["sample_email"] == "Hello!"


class TestTenantHomeBaseCRUD:
    def test_add_and_delete(self, tenant_client):
        client, _, key = tenant_client
        h = _headers(key)

        resp = client.post("/api/v1/tenant/home-bases", json={
            "name": "Test Base", "lat": 39.0, "lng": -86.0,
        }, headers=h)
        assert resp.status_code == 201
        hb_id = resp.json()["id"]

        resp = client.delete(f"/api/v1/tenant/home-bases/{hb_id}", headers=h)
        assert resp.status_code == 204


class TestTenantJobRuleCRUD:
    def test_add_and_delete(self, tenant_client):
        client, _, key = tenant_client
        h = _headers(key)

        resp = client.post("/api/v1/tenant/job-rules", json={
            "category_pattern": "HVAC", "rule_type": "whitelist",
        }, headers=h)
        assert resp.status_code == 201
        rule_id = resp.json()["id"]

        resp = client.delete(f"/api/v1/tenant/job-rules/{rule_id}", headers=h)
        assert resp.status_code == 204

    def test_invalid_rule_type(self, tenant_client):
        client, _, key = tenant_client
        resp = client.post("/api/v1/tenant/job-rules", json={
            "category_pattern": "HVAC", "rule_type": "invalid",
        }, headers=_headers(key))
        assert resp.status_code == 422


class TestTenantSpecialCRUD:
    def test_add_update_delete(self, tenant_client):
        client, _, key = tenant_client
        h = _headers(key)

        resp = client.post("/api/v1/tenant/specials", json={
            "name": "Test Deal", "discount_text": "$10 off",
            "conditions": {"category_contains": "AC"},
        }, headers=h)
        assert resp.status_code == 201
        sp_id = resp.json()["id"]

        resp = client.put(f"/api/v1/tenant/specials/{sp_id}", json={
            "discount_text": "$20 off",
        }, headers=h)
        assert resp.status_code == 200
        assert resp.json()["discount_text"] == "$20 off"

        resp = client.delete(f"/api/v1/tenant/specials/{sp_id}", headers=h)
        assert resp.status_code == 204
