"""Row-Level Security isolation tests.

These tests require PostgreSQL (RLS doesn't exist in SQLite).
They use the docker-compose dev database and are skipped in CI
unless ANGI_TEST_PG_URL is set.

Run: ANGI_TEST_PG_URL=postgresql://angi:angi@localhost:5432/angi_lister pytest tests/test_rls.py -v
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.models import Base, Tenant, AngiMapping, Lead, LeadEvent, OutboundMessage, WebhookReceipt

PG_URL = os.environ.get("ANGI_TEST_PG_URL")
requires_pg = pytest.mark.skipif(not PG_URL, reason="ANGI_TEST_PG_URL not set — needs PostgreSQL for RLS tests")


@pytest.fixture(scope="module")
def pg_engine():
    if not PG_URL:
        pytest.skip("No PostgreSQL URL")
    engine = create_engine(PG_URL, poolclass=NullPool)
    # Ensure tables + RLS policies exist (assumes alembic upgrade head has been run)
    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT COUNT(*) FROM pg_policies WHERE policyname = 'tenant_isolation'"
        )).scalar()
        if result == 0:
            pytest.skip("RLS policies not found — run alembic upgrade head first")
    return engine


@pytest.fixture
def pg_session(pg_engine):
    """A PostgreSQL session with bypass mode, rolled back after each test."""
    conn = pg_engine.connect()
    trans = conn.begin()
    session = Session(bind=conn)
    session.execute(text("SET LOCAL app.current_tenant = '__bypass__'"))
    yield session
    session.close()
    trans.rollback()
    conn.close()


def _make_tenant(session, name, slug):
    t = Tenant(id=str(uuid.uuid4()), name=name, slug=slug, email=f"{slug}@example.com")
    session.add(t)
    session.flush()
    return t


def _make_lead(session, tenant, first_name="Test"):
    receipt = WebhookReceipt(
        headers={}, raw_body={}, auth_valid=True, tenant_id=tenant.id,
    )
    session.add(receipt)
    session.flush()
    lead = Lead(
        correlation_id=str(uuid.uuid4()),
        receipt_id=receipt.id,
        al_account_id="100001",
        tenant_id=tenant.id,
        status="mapped",
        first_name=first_name,
        last_name="User",
        email=f"{first_name.lower()}@example.com",
        phone="5551234567",
        raw_payload={},
        fingerprint=f"{first_name.lower()}@example.com|5551234567|",
    )
    session.add(lead)
    session.flush()
    return lead


@requires_pg
class TestTenantIsolation:
    def test_tenant_a_cannot_see_tenant_b_leads(self, pg_session):
        t_a = _make_tenant(pg_session, "Tenant A", f"tenant-a-{uuid.uuid4().hex[:8]}")
        t_b = _make_tenant(pg_session, "Tenant B", f"tenant-b-{uuid.uuid4().hex[:8]}")
        lead_a = _make_lead(pg_session, t_a, "Alice")
        lead_b = _make_lead(pg_session, t_b, "Bob")

        # Switch to Tenant A's context
        pg_session.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": t_a.id})

        visible = pg_session.query(Lead).all()
        visible_ids = {l.id for l in visible}
        assert lead_a.id in visible_ids
        assert lead_b.id not in visible_ids

    def test_admin_sees_all_tenants(self, pg_session):
        t_a = _make_tenant(pg_session, "Tenant A", f"ta-{uuid.uuid4().hex[:8]}")
        t_b = _make_tenant(pg_session, "Tenant B", f"tb-{uuid.uuid4().hex[:8]}")
        lead_a = _make_lead(pg_session, t_a, "Alice")
        lead_b = _make_lead(pg_session, t_b, "Bob")

        pg_session.execute(text("SET LOCAL app.current_tenant = '__all__'"))

        visible = pg_session.query(Lead).all()
        visible_ids = {l.id for l in visible}
        assert lead_a.id in visible_ids
        assert lead_b.id in visible_ids

    def test_bypass_sees_all(self, pg_session):
        t_a = _make_tenant(pg_session, "Tenant A", f"ta-{uuid.uuid4().hex[:8]}")
        t_b = _make_tenant(pg_session, "Tenant B", f"tb-{uuid.uuid4().hex[:8]}")
        _make_lead(pg_session, t_a)
        _make_lead(pg_session, t_b)

        pg_session.execute(text("SET LOCAL app.current_tenant = '__bypass__'"))

        visible = pg_session.query(Lead).all()
        assert len(visible) >= 2

    def test_events_scoped_to_tenant(self, pg_session):
        t_a = _make_tenant(pg_session, "Tenant A", f"ta-{uuid.uuid4().hex[:8]}")
        t_b = _make_tenant(pg_session, "Tenant B", f"tb-{uuid.uuid4().hex[:8]}")
        lead_a = _make_lead(pg_session, t_a, "Alice")
        lead_b = _make_lead(pg_session, t_b, "Bob")

        evt_a = LeadEvent(lead_id=lead_a.id, tenant_id=t_a.id, event_type="test_a")
        evt_b = LeadEvent(lead_id=lead_b.id, tenant_id=t_b.id, event_type="test_b")
        pg_session.add_all([evt_a, evt_b])
        pg_session.flush()

        pg_session.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": t_a.id})
        events = pg_session.query(LeadEvent).all()
        event_ids = {e.id for e in events}
        assert evt_a.id in event_ids
        assert evt_b.id not in event_ids

    def test_null_tenant_id_invisible_to_tenant(self, pg_session):
        t_a = _make_tenant(pg_session, "Tenant A", f"ta-{uuid.uuid4().hex[:8]}")

        # Receipt with no tenant_id (pre-mapping)
        receipt = WebhookReceipt(
            headers={}, raw_body={}, auth_valid=True, tenant_id=None,
        )
        pg_session.add(receipt)
        pg_session.flush()

        # Switch to tenant context — receipt should be invisible
        pg_session.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": t_a.id})
        visible = pg_session.query(WebhookReceipt).all()
        assert receipt.id not in {r.id for r in visible}

        # Admin should see it
        pg_session.execute(text("SET LOCAL app.current_tenant = '__all__'"))
        visible = pg_session.query(WebhookReceipt).all()
        assert receipt.id in {r.id for r in visible}

    def test_outbound_messages_scoped(self, pg_session):
        t_a = _make_tenant(pg_session, "Tenant A", f"ta-{uuid.uuid4().hex[:8]}")
        t_b = _make_tenant(pg_session, "Tenant B", f"tb-{uuid.uuid4().hex[:8]}")
        lead_a = _make_lead(pg_session, t_a, "Alice")
        lead_b = _make_lead(pg_session, t_b, "Bob")

        msg_a = OutboundMessage(
            lead_id=lead_a.id, tenant_id=t_a.id, channel="email",
            recipient="a@test.com", subject="Test A",
            body_html="test", body_text="test", status="pending",
        )
        msg_b = OutboundMessage(
            lead_id=lead_b.id, tenant_id=t_b.id, channel="email",
            recipient="b@test.com", subject="Test B",
            body_html="test", body_text="test", status="pending",
        )
        pg_session.add_all([msg_a, msg_b])
        pg_session.flush()

        pg_session.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": t_a.id})
        visible = pg_session.query(OutboundMessage).all()
        visible_ids = {m.id for m in visible}
        assert msg_a.id in visible_ids
        assert msg_b.id not in visible_ids
