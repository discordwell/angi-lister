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

from app.models import ConsoleSession, Lead, LeadEvent, Tenant
from app.services.analytics import (
    get_conversion_detail,
    get_conversion_funnel,
    get_tenant_comparison,
)
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


# ---------------------------------------------------------------------------
# The funnel must NOT carry its own conversion_rate
# ---------------------------------------------------------------------------


class TestFunnelOmitsConversionRate:
    """get_conversion_funnel must not compute a second, divergent conversion rate.

    It previously returned ``(booked + won) / (booked + won + lost)`` — an
    event-based rate that excludes in-flight ``mapped`` leads, so it disagreed
    with the canonical ``get_conversion_detail`` / ``get_metrics_summary`` formula
    ``(booked + won) / (mapped + booked + won + lost)``. That value was never
    rendered (the Analytics page reads ``conversion.conversion_rate``), so the bug
    was latent — but a future caller wiring up ``funnel.conversion_rate`` would
    have resurrected the exact dashboard-vs-analytics disagreement this repo has
    been stamping out. Keep it absent.
    """

    def test_funnel_has_no_conversion_rate_key(self, seeded_db):
        t = seeded_db.query(Tenant).first()
        for i, status in enumerate(["mapped", "mapped", "booked", "won", "lost"]):
            _make_lead(seeded_db, t.id, status, i)
        seeded_db.flush()

        funnel = get_conversion_funnel(seeded_db)
        assert "conversion_rate" not in funnel
        # Its real outputs are still present.
        assert "median_speed_to_lead_seconds" in funnel
        assert funnel["outcome_booked"] == 0  # event-based; these leads have no events


# ---------------------------------------------------------------------------
# Speed-to-lead must compute identically across the three surfaces
# ---------------------------------------------------------------------------


def _lead_with_events(db, tenant_id: str, i: int, created_at, sent_at) -> Lead:
    """A mapped lead plus its lead_created / email_sent events at fixed times."""
    lead = _make_lead(db, tenant_id, "mapped", i)
    db.add(LeadEvent(
        lead_id=lead.id, tenant_id=tenant_id,
        event_type="lead_created", payload={}, created_at=created_at,
    ))
    db.add(LeadEvent(
        lead_id=lead.id, tenant_id=tenant_id,
        event_type="email_sent", payload={}, created_at=sent_at,
    ))
    db.flush()
    return lead


class TestSpeedToLeadConsistency:
    """The dashboard tile, the Analytics funnel, and the admin per-tenant table
    all report "median seconds from lead_created to email_sent". They must use one
    formula so the same data reads the same on every page — only the time window
    differs (the dashboard is all-time, the analytics surfaces are windowed). The
    dashboard already dropped clock-skew negatives (sent before created); the two
    analytics surfaces did not, so a single bogus row pulled their median below the
    dashboard's. This pins the shared guard.
    """

    def test_clock_skew_negative_dropped_everywhere(self, seeded_db):
        t = seeded_db.query(Tenant).filter(Tenant.slug == "hoffmann-brothers").first()
        now = utcnow()
        # Two valid leads (+100s, +200s) and one clock-skew lead whose email_sent
        # is recorded 50s BEFORE its lead_created (impossible in normal flow).
        _lead_with_events(
            seeded_db, t.id, 0,
            now - dt.timedelta(hours=2),
            now - dt.timedelta(hours=2) + dt.timedelta(seconds=100),
        )
        _lead_with_events(
            seeded_db, t.id, 1,
            now - dt.timedelta(hours=3),
            now - dt.timedelta(hours=3) + dt.timedelta(seconds=200),
        )
        _lead_with_events(
            seeded_db, t.id, 2,
            now - dt.timedelta(minutes=30),
            now - dt.timedelta(minutes=30) - dt.timedelta(seconds=50),
        )
        seeded_db.flush()

        # median over valid deltas only = median([100, 200]) = 150.
        # If the -50 leaked in, median([-50, 100, 200]) would be 100.
        assert get_metrics_summary(seeded_db)["median_speed_to_lead_seconds"] == 150.0
        assert get_conversion_funnel(seeded_db)["median_speed_to_lead_seconds"] == 150.0

        comparison = get_tenant_comparison(seeded_db)
        row = next(r for r in comparison if r["tenant_slug"] == "hoffmann-brothers")
        assert row["speed_to_lead"] == 150.0

    def test_medians_rounded_consistently(self, seeded_db):
        """All three surfaces round to 2 dp, so they agree on the raw value — not
        just the rendered %.0f/%.1f — the same rule conversion rate follows."""
        t = seeded_db.query(Tenant).filter(Tenant.slug == "hoffmann-brothers").first()
        now = utcnow()
        # Three valid leads; the middle delta is fractional (100.333…s), so the
        # median is that value and 2-dp rounding is observable: 100.33 (unrounded
        # the funnel/admin would report 100.333333 and disagree with the tile).
        _lead_with_events(
            seeded_db, t.id, 0,
            now - dt.timedelta(hours=2),
            now - dt.timedelta(hours=2) + dt.timedelta(seconds=50),
        )
        _lead_with_events(
            seeded_db, t.id, 1,
            now - dt.timedelta(hours=3),
            now - dt.timedelta(hours=3) + dt.timedelta(seconds=100, microseconds=333333),
        )
        _lead_with_events(
            seeded_db, t.id, 2,
            now - dt.timedelta(hours=4),
            now - dt.timedelta(hours=4) + dt.timedelta(seconds=200),
        )
        seeded_db.flush()

        expected = 100.33  # round(100.333333, 2)
        assert get_metrics_summary(seeded_db)["median_speed_to_lead_seconds"] == expected
        assert get_conversion_funnel(seeded_db)["median_speed_to_lead_seconds"] == expected
        comparison = get_tenant_comparison(seeded_db)
        row = next(r for r in comparison if r["tenant_slug"] == "hoffmann-brothers")
        assert row["speed_to_lead"] == expected
