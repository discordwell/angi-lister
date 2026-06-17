"""Tests for analytics conversion-rate computation and display.

Guards two related defects that made the console *Analytics* page disagree with
the *Dashboard* for the same data:

1. ``get_conversion_detail`` excluded leads still in ``mapped`` (in-flight)
   status from the conversion-rate denominator, while ``get_metrics_summary``
   (the dashboard KPI) and ARCHITECTURE.md define the canonical formula as
   ``(booked + won) / (mapped + booked + won + lost)``. So the two pages showed
   different "Conversion Rate" numbers for identical data.

2. ``analytics.html`` rendered the 0–1 conversion *fraction* without scaling it
   to a percentage — ``0.5`` displayed as ``0.5%`` instead of ``50.0%`` — and
   colored the tile with thresholds (``> 30`` / ``> 15``) that a fraction can
   never reach, so the tile was *always* red ("danger"). Every other rate in the
   app (dashboard.html, analytics_admin.html) multiplies by 100 and uses
   fraction-scale thresholds.
"""

import datetime as dt
import secrets
import uuid

import pytest
from fastapi.testclient import TestClient

from app.models import ConsoleSession, Lead, Tenant
from app.services.analytics import get_conversion_detail
from app.services.auth import _hash, _sign_cookie
from app.services.metrics import get_metrics_summary
from app.utils import utcnow


def _make_lead(db, tenant_id: str, status: str, i: int) -> Lead:
    lead = Lead(
        correlation_id=f"conv-{status}-{i}-{uuid.uuid4()}",
        tenant_id=tenant_id,
        al_account_id="100001",
        status=status,
        first_name="Test",
        last_name=status.title(),
        email=f"{status}{i}@example.com",
        phone="5550000000",
        raw_payload={},
    )
    db.add(lead)
    db.flush()
    return lead


# ---------------------------------------------------------------------------
# Service-level: conversion_rate formula must match the documented KPI
# ---------------------------------------------------------------------------

class TestConversionDetailFormula:
    def test_includes_mapped_in_denominator(self, seeded_db):
        """(booked + won) / (mapped + booked + won + lost) — mapped counts."""
        t = seeded_db.query(Tenant).first()
        for i, status in enumerate(["mapped", "booked", "won", "lost"]):
            _make_lead(seeded_db, t.id, status, i)
        seeded_db.flush()

        detail = get_conversion_detail(seeded_db)
        # 2 / 4 = 0.5 (NOT 2/3 = 0.667, which excluded mapped)
        assert detail["conversion_rate"] == 0.5

    def test_matches_dashboard_kpi(self, seeded_db):
        """Analytics page and dashboard must report the same conversion rate."""
        t = seeded_db.query(Tenant).first()
        for i, status in enumerate(["mapped", "mapped", "booked", "won", "lost"]):
            _make_lead(seeded_db, t.id, status, i)
        seeded_db.flush()

        detail = get_conversion_detail(seeded_db)
        summary = get_metrics_summary(seeded_db)
        assert detail["conversion_rate"] == summary["conversion_rate"]
        # 2 / (2 mapped + booked + won + lost) = 2/5 = 0.4
        assert detail["conversion_rate"] == 0.4

    def test_none_when_no_actionable_leads(self, seeded_db):
        assert get_conversion_detail(seeded_db)["conversion_rate"] is None

    def test_only_resolved_is_full_rate(self, seeded_db):
        """No mapped leads → denominator is just resolved leads."""
        t = seeded_db.query(Tenant).first()
        for i, status in enumerate(["won", "booked"]):
            _make_lead(seeded_db, t.id, status, i)
        seeded_db.flush()
        # 2 / 2 = 1.0
        assert get_conversion_detail(seeded_db)["conversion_rate"] == 1.0


# ---------------------------------------------------------------------------
# Template-level: the rendered Analytics page must show a real percentage
# ---------------------------------------------------------------------------

@pytest.fixture
def analytics_client(seeded_db):
    """An authenticated console client whose tenant has mixed-outcome leads."""
    from app.main import create_app
    from app.db.session import get_db, get_bypass_db
    from app.routers.console import get_console_db
    import app.routers.console as console_mod

    t = seeded_db.query(Tenant).first()
    # 1 mapped, 1 booked, 1 won, 1 lost → conversion = 2/4 = 0.5 → "50.0%"
    for i, status in enumerate(["mapped", "booked", "won", "lost"]):
        _make_lead(seeded_db, t.id, status, i)
    seeded_db.flush()

    raw_token = "sess_" + secrets.token_urlsafe(32)
    session = ConsoleSession(
        tenant_id=t.id,
        email="analytics@test.example.com",
        session_token_hash=_hash(raw_token),
        expires_at=utcnow() + dt.timedelta(days=7),
    )
    seeded_db.add(session)
    seeded_db.flush()

    cookie = _sign_cookie({
        "token": raw_token,
        "email": session.email,
        "tenant_id": t.id,
        "exp": int(session.expires_at.timestamp() * 1000),
    })

    app = create_app()

    def override():
        yield seeded_db

    app.dependency_overrides[get_db] = override
    app.dependency_overrides[get_bypass_db] = override
    app.dependency_overrides[get_console_db] = override

    original_sl = console_mod.SessionLocal
    console_mod.SessionLocal = lambda: seeded_db
    try:
        with TestClient(app, cookies={"angi_session": cookie}) as c:
            yield c
    finally:
        console_mod.SessionLocal = original_sl


def _conversion_kpi_tile(html: str) -> str:
    """Return the inner HTML of the first 'Conversion Rate' KPI tile."""
    # The metric-value div precedes its metric-label, so the chunk between the
    # last "metric-tile" marker and the first "Conversion Rate" label is the tile.
    return html.split("Conversion Rate")[0].rsplit("metric-tile", 1)[-1]


class TestAnalyticsConversionDisplay:
    def test_conversion_rate_shown_as_percentage(self, analytics_client):
        resp = analytics_client.get("/console/analytics")
        assert resp.status_code == 200
        tile = _conversion_kpi_tile(resp.text)
        # Scaled to a percentage, not left as the raw 0.5 fraction.
        assert "50.0%" in tile
        # The conversion rate renders in two places (KPI tile + outcomes donut);
        # both must scale by 100. Nothing else in this seed produces "0.5%".
        assert resp.text.count("50.0%") >= 2
        assert "0.5%" not in resp.text

    def test_conversion_tile_color_reflects_rate(self, analytics_client):
        resp = analytics_client.get("/console/analytics")
        tile = _conversion_kpi_tile(resp.text)
        # 0.5 clears the 0.30 "ok" threshold; the old >30 test made it always red.
        assert "metric-value ok" in tile
        assert "danger" not in tile
