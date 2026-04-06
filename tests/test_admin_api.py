"""Tests for the admin API."""

import pytest

from app.models import ApiKey, Tenant
from app.services.api_auth import generate_api_key


@pytest.fixture
def admin_key(db):
    """Create an admin API key (not bound to any tenant)."""
    # Admin keys need a tenant for audit purposes in our schema,
    # but is_admin=True grants cross-tenant access.
    # Actually, admin keys have tenant_id=None per our design.
    _, raw_key = generate_api_key(db, tenant_id=None, name="admin key", is_admin=True)
    db.flush()
    return raw_key


@pytest.fixture
def admin_client(admin_key, db):
    from app.main import create_app
    from app.db.session import get_db, get_bypass_db

    app = create_app()

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    app.dependency_overrides[get_bypass_db] = override

    import app.services.api_auth as auth_mod
    original = auth_mod.SessionLocal
    auth_mod.SessionLocal = lambda: db

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c, admin_key

    auth_mod.SessionLocal = original


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


class TestAdminAuth:
    def test_no_auth_returns_401(self, admin_client):
        client, _ = admin_client
        resp = client.get("/api/v1/admin/tenants")
        assert resp.status_code == 401

    def test_tenant_key_returns_403(self, admin_client, db):
        client, _ = admin_client
        t = Tenant(name="Blocked", slug="blocked-t", email="b@b.com")
        db.add(t)
        db.flush()
        _, tenant_key = generate_api_key(db, tenant_id=t.id, name="t key")
        db.flush()

        resp = client.get("/api/v1/admin/tenants", headers=_headers(tenant_key))
        assert resp.status_code == 403


class TestAdminTenants:
    def test_list_tenants(self, admin_client, db):
        client, key = admin_client
        db.add(Tenant(name="T1", slug="t1-slug", email="t1@e.com"))
        db.flush()

        resp = client.get("/api/v1/admin/tenants", headers=_headers(key))
        assert resp.status_code == 200
        assert any(t["name"] == "T1" for t in resp.json())

    def test_create_tenant(self, admin_client):
        client, key = admin_client
        resp = client.post("/api/v1/admin/tenants", json={
            "name": "New Tenant", "slug": "new-tenant",
        }, headers=_headers(key))
        assert resp.status_code == 201
        assert resp.json()["name"] == "New Tenant"

    def test_create_duplicate_slug_409(self, admin_client, db):
        client, key = admin_client
        db.add(Tenant(name="Existing", slug="dupe-slug", email="d@d.com"))
        db.flush()

        resp = client.post("/api/v1/admin/tenants", json={
            "name": "Another", "slug": "dupe-slug",
        }, headers=_headers(key))
        assert resp.status_code == 409

    def test_update_tenant(self, admin_client, db):
        client, key = admin_client
        t = Tenant(name="Orig", slug="upd-slug", email="u@u.com")
        db.add(t)
        db.flush()

        resp = client.put(f"/api/v1/admin/tenants/{t.id}", json={
            "name": "Updated",
        }, headers=_headers(key))
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"


class TestAdminApiKeys:
    def test_create_and_list_keys(self, admin_client, db):
        client, key = admin_client
        t = Tenant(name="KeyTest", slug="keytest", email="k@k.com")
        db.add(t)
        db.flush()

        resp = client.post(f"/api/v1/admin/tenants/{t.id}/api-keys", json={
            "name": "CLI key",
        }, headers=_headers(key))
        assert resp.status_code == 201
        data = resp.json()
        assert "raw_key" in data
        assert data["raw_key"].startswith("angi_")

        resp = client.get(f"/api/v1/admin/tenants/{t.id}/api-keys", headers=_headers(key))
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_revoke_key(self, admin_client, db):
        client, key = admin_client
        t = Tenant(name="RevokeTest", slug="revoketest", email="r@r.com")
        db.add(t)
        db.flush()

        resp = client.post(f"/api/v1/admin/tenants/{t.id}/api-keys", json={
            "name": "to revoke",
        }, headers=_headers(key))
        key_id = resp.json()["id"]

        resp = client.delete(
            f"/api/v1/admin/tenants/{t.id}/api-keys/{key_id}",
            headers=_headers(key),
        )
        assert resp.status_code == 204


class TestAdminMappings:
    def test_create_mapping(self, admin_client, db):
        client, key = admin_client
        t = Tenant(name="MapTest", slug="maptest", email="m@m.com")
        db.add(t)
        db.flush()

        resp = client.post(f"/api/v1/admin/tenants/{t.id}/mappings", json={
            "al_account_id": "999999",
        }, headers=_headers(key))
        assert resp.status_code == 201
        assert resp.json()["al_account_id"] == "999999"

    def test_duplicate_mapping_409(self, admin_client, db):
        client, key = admin_client
        from app.models import AngiMapping

        t = Tenant(name="DupMap", slug="dupmap", email="dm@dm.com")
        db.add(t)
        db.flush()
        db.add(AngiMapping(al_account_id="888888", tenant_id=t.id))
        db.flush()

        resp = client.post(f"/api/v1/admin/tenants/{t.id}/mappings", json={
            "al_account_id": "888888",
        }, headers=_headers(key))
        assert resp.status_code == 409


class TestAdminMetrics:
    def test_global_metrics(self, admin_client):
        client, key = admin_client
        resp = client.get("/api/v1/admin/metrics", headers=_headers(key))
        assert resp.status_code == 200
        assert "total_leads_24h" in resp.json()
