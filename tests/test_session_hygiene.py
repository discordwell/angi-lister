"""Production-topology tests for session lifecycle and RLS context handling.

Unlike the rest of the suite, these tests do NOT override the app's DB
dependencies: requests run through the real get_bypass_db / require_tenant /
require_admin wiring against the file-backed SQLite database configured in
conftest (DATABASE_URL=sqlite:///./test.db). This catches bugs that the
dependency-override fixtures mask — e.g. changes applied to one session but
committed on another, or auth bookkeeping rolled back when the request's
sessions close.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.db.session import SessionLocal, engine, set_tenant
from app.main import create_app
from app.models import AngiMapping, ApiKey, Base, Tenant, TenantHomeBase
from app.services.api_auth import generate_api_key


@pytest.fixture
def real_app_client():
    """TestClient against an app with NO dependency overrides.

    Seeds one tenant plus a tenant key and an admin key on the real engine,
    and tears the schema down afterwards.
    """
    Base.metadata.create_all(bind=engine)
    try:
        db = SessionLocal()
        try:
            tenant = Tenant(
                name="Hygiene Tenant", slug="hygiene-tenant",
                email="hygiene@example.com", phone="5550001111",
                brand_color="#222222", timezone="America/Chicago",
            )
            db.add(tenant)
            db.flush()
            tenant_id = tenant.id
            _, tenant_key = generate_api_key(db, tenant_id=tenant_id, name="hygiene key")
            _, admin_key = generate_api_key(db, tenant_id=None, name="hygiene admin", is_admin=True)
            db.commit()
        finally:
            db.close()

        with TestClient(create_app()) as client:
            yield client, tenant_id, tenant_key, admin_key
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


class TestTenantSessionHygiene:
    def test_config_update_persists_across_requests(self, real_app_client):
        """PUT /config must commit on the session it modifies.

        Regression: updates were applied to the auth session's tenant object
        but committed on the handler session, so they were silently rolled
        back when the request ended.
        """
        client, tenant_id, key, _ = real_app_client
        resp = client.put(
            "/api/v1/tenant/config",
            json={"sample_email": "Persisted hello", "personalization_enabled": True},
            headers=_auth(key),
        )
        assert resp.status_code == 200
        assert resp.json()["sample_email"] == "Persisted hello"

        # A fresh request (fresh sessions) must see the update.
        resp = client.get("/api/v1/tenant/config", headers=_auth(key))
        assert resp.status_code == 200
        assert resp.json()["sample_email"] == "Persisted hello"
        assert resp.json()["personalization_enabled"] is True

        # And it must be durably in the database, not just request state.
        db = SessionLocal()
        try:
            t = db.get(Tenant, tenant_id)
            assert t.sample_email == "Persisted hello"
            assert t.personalization_enabled is True
        finally:
            db.close()

    def test_home_base_create_visible_in_new_session(self, real_app_client):
        client, tenant_id, key, _ = real_app_client
        resp = client.post(
            "/api/v1/tenant/home-bases",
            json={"name": "HQ", "lat": 38.6, "lng": -90.2},
            headers=_auth(key),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "HQ"
        assert body["id"]

        db = SessionLocal()
        try:
            row = db.get(TenantHomeBase, body["id"])
            assert row is not None
            assert row.tenant_id == tenant_id
        finally:
            db.close()

    def test_api_key_last_used_at_is_persisted(self, real_app_client):
        """Regression: last_used_at was flushed on the auth session but never
        committed, so key usage tracking was silently lost."""
        client, _, key, _ = real_app_client
        resp = client.get("/api/v1/tenant/me", headers=_auth(key))
        assert resp.status_code == 200

        db = SessionLocal()
        try:
            record = db.query(ApiKey).filter(ApiKey.is_admin.is_(False)).one()
            assert record.last_used_at is not None
        finally:
            db.close()


class TestAdminSessionHygiene:
    def test_admin_create_api_key_roundtrip(self, real_app_client):
        client, tenant_id, _, admin_key = real_app_client
        resp = client.post(
            f"/api/v1/admin/tenants/{tenant_id}/api-keys",
            json={"name": "issued key"},
            headers=_auth(admin_key),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["raw_key"].startswith("angi_")

        db = SessionLocal()
        try:
            assert db.get(ApiKey, body["id"]) is not None
        finally:
            db.close()

    def test_admin_add_mapping_roundtrip(self, real_app_client):
        client, tenant_id, _, admin_key = real_app_client
        resp = client.post(
            f"/api/v1/admin/tenants/{tenant_id}/mappings",
            json={"al_account_id": "987654"},
            headers=_auth(admin_key),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["al_account_id"] == "987654"

        db = SessionLocal()
        try:
            mapping = db.get(AngiMapping, body["id"])
            assert mapping is not None
            assert mapping.tenant_id == tenant_id
        finally:
            db.close()


class TestSetTenantScoping:
    def test_noop_on_sqlite(self):
        db = SessionLocal()
        try:
            set_tenant(db, "some-tenant", session_scope=True)
            assert "rls_tenant" not in db.info
        finally:
            db.close()

    def test_session_scope_registers_transaction_listener(self):
        """session_scope must re-apply SET LOCAL on every new transaction, so
        tenant context survives commits without anything persisting on the
        pooled connection."""
        db = SessionLocal()
        try:
            bind = MagicMock()
            bind.dialect.name = "postgresql"
            with patch.object(db, "get_bind", return_value=bind), \
                 patch.object(db, "execute") as execute:
                set_tenant(db, "tenant-a", session_scope=True)
                assert db.info["rls_tenant"] == "tenant-a"
                listener = db.info["rls_tenant_listener"]
                assert event.contains(db, "after_begin", listener)
                # Applied once to the current transaction
                assert execute.call_count == 1

                # Re-pinning the same tenant is a no-op (worker re-set path).
                set_tenant(db, "tenant-a", session_scope=True)
                assert execute.call_count == 1

                # The listener issues SET LOCAL on each new transaction.
                conn = MagicMock()
                listener(db, None, conn)
                assert conn.execute.call_args[0][1] == {"tid": "tenant-a"}

                # Pinning a different tenant replaces the previous listener.
                set_tenant(db, "tenant-b", session_scope=True)
                assert db.info["rls_tenant"] == "tenant-b"
                assert not event.contains(db, "after_begin", listener)
                assert event.contains(db, "after_begin", db.info["rls_tenant_listener"])
        finally:
            db.close()
