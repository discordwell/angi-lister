"""Tests for the conversion feedback loop — outcome setting and metrics."""

import uuid

from app.models import Lead, LeadEvent
from app.services.metrics import get_metrics_summary
from tests.conftest import SAMPLE_LEAD


def _create_mapped_lead(db, tenant_id, correlation_id=None):
    """Helper: create a lead with status='mapped' for a given tenant."""
    lead = Lead(
        correlation_id=correlation_id or str(uuid.uuid4()),
        tenant_id=tenant_id,
        al_account_id="100001",
        status="mapped",
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
        phone="5551234567",
        raw_payload=SAMPLE_LEAD,
    )
    db.add(lead)
    db.flush()
    return lead


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

def test_set_outcome_booked(seeded_db, seeded_client):
    """POST outcome=booked on a mapped lead should update status."""
    from app.models import Tenant
    t = seeded_db.query(Tenant).first()
    lead = _create_mapped_lead(seeded_db, t.id)
    seeded_db.commit()

    resp = seeded_client.post(
        f"/api/v1/leads/{lead.id}/outcome",
        json={"outcome": "booked"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "booked"
    assert data["previous_status"] == "mapped"


def test_set_outcome_won(seeded_db, seeded_client):
    from app.models import Tenant
    t = seeded_db.query(Tenant).first()
    lead = _create_mapped_lead(seeded_db, t.id)
    seeded_db.commit()

    resp = seeded_client.post(
        f"/api/v1/leads/{lead.id}/outcome",
        json={"outcome": "won"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "won"


def test_set_outcome_lost(seeded_db, seeded_client):
    from app.models import Tenant
    t = seeded_db.query(Tenant).first()
    lead = _create_mapped_lead(seeded_db, t.id)
    seeded_db.commit()

    resp = seeded_client.post(
        f"/api/v1/leads/{lead.id}/outcome",
        json={"outcome": "lost"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "lost"


def test_set_outcome_invalid_value(seeded_db, seeded_client):
    """Invalid outcome value should return 422."""
    from app.models import Tenant
    t = seeded_db.query(Tenant).first()
    lead = _create_mapped_lead(seeded_db, t.id)
    seeded_db.commit()

    resp = seeded_client.post(
        f"/api/v1/leads/{lead.id}/outcome",
        json={"outcome": "invalid"},
    )
    assert resp.status_code == 422


def test_set_outcome_lead_not_found(seeded_client):
    """Non-existent lead should return 404."""
    resp = seeded_client.post(
        "/api/v1/leads/nonexistent-id/outcome",
        json={"outcome": "booked"},
    )
    assert resp.status_code == 404


def test_set_outcome_unmapped_lead_rejected(seeded_db, seeded_client):
    """Cannot set outcome on an unmapped lead."""
    lead = Lead(
        correlation_id=str(uuid.uuid4()),
        al_account_id="999999",
        status="unmapped",
        first_name="Test",
        last_name="User",
        email="test@example.com",
        phone="5550000000",
        raw_payload={},
    )
    seeded_db.add(lead)
    seeded_db.commit()

    resp = seeded_client.post(
        f"/api/v1/leads/{lead.id}/outcome",
        json={"outcome": "booked"},
    )
    assert resp.status_code == 409


def test_set_outcome_creates_event(seeded_db, seeded_client):
    """Setting outcome should create a LeadEvent with correct type and payload."""
    from app.models import Tenant
    t = seeded_db.query(Tenant).first()
    lead = _create_mapped_lead(seeded_db, t.id)
    seeded_db.commit()

    seeded_client.post(
        f"/api/v1/leads/{lead.id}/outcome",
        json={"outcome": "won", "notes": "Customer confirmed appointment"},
    )

    events = (
        seeded_db.query(LeadEvent)
        .filter(LeadEvent.lead_id == lead.id, LeadEvent.event_type == "outcome_won")
        .all()
    )
    assert len(events) == 1
    assert events[0].payload["notes"] == "Customer confirmed appointment"
    assert events[0].payload["previous_status"] == "mapped"


def test_set_outcome_allows_change(seeded_db, seeded_client):
    """Should be able to change from one outcome to another."""
    from app.models import Tenant
    t = seeded_db.query(Tenant).first()
    lead = _create_mapped_lead(seeded_db, t.id)
    seeded_db.commit()

    # Set to booked
    resp1 = seeded_client.post(
        f"/api/v1/leads/{lead.id}/outcome",
        json={"outcome": "booked"},
    )
    assert resp1.status_code == 200

    # Change to won
    resp2 = seeded_client.post(
        f"/api/v1/leads/{lead.id}/outcome",
        json={"outcome": "won"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "won"
    assert resp2.json()["previous_status"] == "booked"


# ---------------------------------------------------------------------------
# Metrics tests
# ---------------------------------------------------------------------------

def test_conversion_rate_metric(seeded_db):
    """Conversion rate should be (booked+won) / (mapped+booked+won+lost)."""
    from app.models import Tenant
    t = seeded_db.query(Tenant).first()

    # Create 4 leads: 1 mapped, 1 booked, 1 won, 1 lost
    for status in ["mapped", "booked", "won", "lost"]:
        lead = Lead(
            correlation_id=str(uuid.uuid4()),
            tenant_id=t.id,
            al_account_id="100001",
            status=status,
            first_name="Test",
            last_name=status.title(),
            email=f"{status}@example.com",
            phone="5550000000",
            raw_payload={},
        )
        seeded_db.add(lead)
    seeded_db.flush()

    metrics = get_metrics_summary(seeded_db)
    # (booked + won) / (mapped + booked + won + lost) = 2/4 = 0.5
    assert metrics["conversion_rate"] == 0.5


def test_conversion_rate_no_mapped_leads(seeded_db):
    """Conversion rate should be None when no actionable leads exist."""
    # seeded_db has tenants but no leads
    metrics = get_metrics_summary(seeded_db)
    assert metrics["conversion_rate"] is None
