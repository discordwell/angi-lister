"""Tests for the dashboard 'Delivery Rate' KPI (get_metrics_summary).

Guards a defect in the same class as the conversion-rate fix: the same metric
computed with two different formulas on two pages.

    Dashboard (get_metrics_summary):     sent / (every non-simulated message)
    Admin (get_tenant_comparison):       sent / (sent + failed)
    System health (get_system_health):   failure rate over (sent + failed)

The dashboard was the lone outlier. Its denominator counted messages that were
never a completed send attempt — in-flight 'pending'/'generating' and the
personalization engine's intentional non-sends 'declined'/'skipped' — so a queue
backlog or a tenant that declines/skips most leads showed a spuriously low
"Delivery Rate" (and the tile read red) even when every message actually
attempted had been delivered. Same data, same "Delivery Rate" label, two numbers.

Canonical: delivery rate counts only terminal send outcomes, sent / (sent +
failed). These tests pin the formula and that the dashboard now agrees with the
admin page for the same (non-simulated) data.
"""

import datetime as dt
import uuid

from app.models import Lead, OutboundMessage, Tenant
from app.services.analytics import get_system_health, get_tenant_comparison
from app.services.metrics import get_metrics_summary
from app.utils import utcnow


def _lead(db, tenant_id: str) -> Lead:
    lead = Lead(
        correlation_id=f"dr-{uuid.uuid4()}",
        tenant_id=tenant_id,
        al_account_id="100001",
        status="mapped",
        first_name="Test",
        last_name="Lead",
        email="t@example.com",
        phone="5550000000",
        raw_payload={},
    )
    db.add(lead)
    db.flush()
    return lead


def _msg(db, lead: Lead, status: str, *, is_simulated: bool = False) -> OutboundMessage:
    msg = OutboundMessage(
        lead_id=lead.id,
        tenant_id=lead.tenant_id,
        recipient=lead.email,
        subject="s",
        body_html="<p>x</p>",
        body_text="x",
        status=status,
        is_simulated=is_simulated,
    )
    db.add(msg)
    db.flush()
    return msg


def _seed_messages(db, tenant_id: str, counts: dict[str, int], *, is_simulated: bool = False):
    lead = _lead(db, tenant_id)
    for status, n in counts.items():
        for _ in range(n):
            _msg(db, lead, status, is_simulated=is_simulated)


class TestDeliveryRateFormula:
    def test_excludes_non_attempt_statuses(self, seeded_db):
        """Only sent + failed count; pending/generating/declined/skipped do not."""
        t = seeded_db.query(Tenant).first()
        _seed_messages(seeded_db, t.id, {
            "sent": 3,
            "failed": 1,
            # None of these is a completed send attempt — they must not count.
            "pending": 5,
            "generating": 2,
            "declined": 4,
            "skipped": 6,
        })
        seeded_db.flush()

        rate = get_metrics_summary(seeded_db, tenant_id=t.id)["delivery_success_rate"]
        # 3 / (3 sent + 1 failed) = 0.75. The old formula divided by all 21
        # messages -> 3/21 = 0.14, a spuriously low rate that colored the tile red.
        assert rate == 0.75

    def test_matches_admin_delivery_rate(self, seeded_db):
        """Dashboard and admin page must report the same delivery rate."""
        t = seeded_db.query(Tenant).first()
        # Non-simulated messages: the real delivery population both pages measure.
        _seed_messages(seeded_db, t.id, {"sent": 4, "failed": 1, "pending": 3, "skipped": 2})
        seeded_db.flush()

        dash = get_metrics_summary(seeded_db, tenant_id=t.id)["delivery_success_rate"]
        admin_by_name = {row["tenant_name"]: row for row in get_tenant_comparison(seeded_db)}
        admin = admin_by_name[t.name]["delivery_rate"]

        # 4 / (4 + 1) = 0.8 on both pages, for the same data.
        assert dash == admin == 0.8

    def test_none_when_no_terminal_attempts(self, seeded_db):
        """Only in-flight / non-attempt messages -> no rate to show (— not 0%)."""
        t = seeded_db.query(Tenant).first()
        _seed_messages(seeded_db, t.id, {"pending": 3, "declined": 2, "skipped": 1})
        seeded_db.flush()

        assert get_metrics_summary(seeded_db, tenant_id=t.id)["delivery_success_rate"] is None

    def test_simulated_excluded(self, seeded_db):
        """Simulated sends are a local/demo artifact and never count."""
        t = seeded_db.query(Tenant).first()
        # Every real (non-simulated) send succeeded; a batch of simulated failures
        # must not drag the real delivery rate below 100%.
        _seed_messages(seeded_db, t.id, {"sent": 2}, is_simulated=False)
        _seed_messages(seeded_db, t.id, {"failed": 5}, is_simulated=True)
        seeded_db.flush()

        assert get_metrics_summary(seeded_db, tenant_id=t.id)["delivery_success_rate"] == 1.0


class TestAdminDeliveryRatePopulation:
    """get_tenant_comparison must measure the same population as the dashboard.

    Its delivery rate previously counted simulated messages, while its sibling
    personalization rate (and the dashboard delivery tile) excluded them — so
    "delivery rate" meant different things on the two pages. They now agree even
    when simulated traffic is present.
    """

    def test_admin_delivery_rate_excludes_simulated(self, seeded_db):
        t = seeded_db.query(Tenant).first()
        _seed_messages(seeded_db, t.id, {"sent": 4, "failed": 1}, is_simulated=False)
        # Simulated 'sent' would inflate the admin rate to 14/15 = 0.933 if counted.
        _seed_messages(seeded_db, t.id, {"sent": 10}, is_simulated=True)
        seeded_db.flush()

        admin_by_name = {row["tenant_name"]: row for row in get_tenant_comparison(seeded_db)}
        dash = get_metrics_summary(seeded_db, tenant_id=t.id)["delivery_success_rate"]

        # Both measure only the real sends: 4 / (4 + 1) = 0.8.
        assert admin_by_name[t.name]["delivery_rate"] == 0.8
        assert dash == admin_by_name[t.name]["delivery_rate"]


def _sys_msg(
    db,
    tenant_id: str,
    status: str,
    *,
    is_simulated: bool = False,
    queued_ago_hours: float = 0.0,
    sent_ago_hours: float | None = None,
) -> OutboundMessage:
    """Create a message with explicit timestamps for get_system_health tests.

    Mirrors prod, where the worker sets status='sent' and sent_at together
    (email.py). A 'sent' message therefore carries a sent_at; by default it
    equals the queue time (near-instant send), but sent_ago_hours can override
    it to model a slow/retried send queued outside the 24h window.
    """
    lead = _lead(db, tenant_id)
    sent_at = None
    if status == "sent":
        ago = sent_ago_hours if sent_ago_hours is not None else queued_ago_hours
        sent_at = utcnow() - dt.timedelta(hours=ago)
    msg = OutboundMessage(
        lead_id=lead.id,
        tenant_id=tenant_id,
        recipient=lead.email,
        subject="s",
        body_html="<p>x</p>",
        body_text="x",
        status=status,
        is_simulated=is_simulated,
        queued_at=utcnow() - dt.timedelta(hours=queued_ago_hours),
        sent_at=sent_at,
    )
    db.add(msg)
    db.flush()
    return msg


class TestSystemHealthDeliveryPopulation:
    """get_system_health.email_failure_rate_24h is the complement of the canonical
    delivery rate, so it must measure the same population (non-simulated) over the
    same window cohort (queued_at). It previously counted simulated sends and
    anchored 'sent' on sent_at while 'failed' used queued_at — two defects that
    let demo traffic and a windowing asymmetry distort the system-health verdict.
    The function had no test at all before this; these pin both fixes.
    """

    def test_simulated_sends_dont_mask_failure_rate(self, seeded_db):
        """A pile of simulated sends must not dilute a real failure spike below
        the 0.1 'critical' threshold — that would hide an actual email outage."""
        t = seeded_db.query(Tenant).first()
        # Real traffic: half the attempts failed — unambiguously critical.
        _sys_msg(seeded_db, t.id, "sent")
        _sys_msg(seeded_db, t.id, "sent")
        _sys_msg(seeded_db, t.id, "failed")
        _sys_msg(seeded_db, t.id, "failed")
        # Demo/test traffic from the console 'simulate' button — must be ignored.
        for _ in range(16):
            _sys_msg(seeded_db, t.id, "sent", is_simulated=True)
        seeded_db.flush()

        health = get_system_health(seeded_db)
        # Real failure rate is 2 / (2 + 2) = 0.5. With simulated sends counted
        # (the old code) it was 2 / (2 + 18) = 0.1 — exactly at, not above, the
        # threshold, so overall_health read 'warn' instead of 'critical'.
        assert health["email_failure_rate_24h"] == 0.5
        assert health["overall_health"] == "critical"

    def test_failure_rate_is_complement_of_admin_delivery_rate(self, seeded_db):
        """For the same data, the admin table's delivery_rate and system health's
        failure_rate must sum to 1 — they measure one population two ways."""
        t = seeded_db.query(Tenant).first()
        _sys_msg(seeded_db, t.id, "sent")
        _sys_msg(seeded_db, t.id, "sent")
        _sys_msg(seeded_db, t.id, "sent")
        _sys_msg(seeded_db, t.id, "sent")
        _sys_msg(seeded_db, t.id, "failed")
        # Simulated noise both functions must exclude.
        for _ in range(10):
            _sys_msg(seeded_db, t.id, "sent", is_simulated=True)
        seeded_db.flush()

        health = get_system_health(seeded_db)
        admin = {row["tenant_name"]: row for row in get_tenant_comparison(seeded_db)}
        delivery_rate = admin[t.name]["delivery_rate"]

        # Non-simulated: 4 sent, 1 failed. delivery 4/5=0.8, failure 1/5=0.2.
        assert delivery_rate == 0.8
        assert health["email_failure_rate_24h"] == 0.2
        assert round(health["email_failure_rate_24h"] + delivery_rate, 6) == 1.0

    def test_window_anchor_excludes_old_queued_from_both_sides(self, seeded_db):
        """The 24h cohort is 'messages queued in the last 24h'. A send queued
        before the window must be excluded even if its sent_at is recent — the
        same way a failure queued before the window is already excluded. Anchoring
        'sent' on sent_at let an out-of-window send leak in and understate the
        failure rate."""
        t = seeded_db.query(Tenant).first()
        # Queued 30h ago (outside the window) but only just delivered. The old
        # sent_at anchor counted this; the queued_at cohort correctly drops it.
        _sys_msg(seeded_db, t.id, "sent", queued_ago_hours=30, sent_ago_hours=1)
        # The only message actually queued inside the window — and it failed.
        _sys_msg(seeded_db, t.id, "failed", queued_ago_hours=1)
        seeded_db.flush()

        health = get_system_health(seeded_db)
        # In-window cohort is just the one failure: 1 / (0 + 1) = 1.0. The old
        # mixed anchors gave 1 / (1 + 1) = 0.5.
        assert health["email_failure_rate_24h"] == 1.0
        assert health["overall_health"] == "critical"
