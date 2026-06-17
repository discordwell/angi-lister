"""Tests for metrics service query shape — guards against N+1 regressions.

get_recent_leads and get_duplicate_pairs both iterate ORM rows and read across
relationships (lead.tenant, m.lead, m.original). Those relationships are lazy by
default, so without eager loading the SELECT count grows with the number of rows
(N+1). These tests assert the count stays bounded regardless of row count.
"""

import uuid
from contextlib import contextmanager

from sqlalchemy import event

from app.models import WebhookReceipt
from app.schemas.angi import AngiLeadPayload
from app.services.ingestion import process_lead
from app.services.metrics import get_duplicate_pairs, get_recent_leads
from tests.conftest import SAMPLE_LEAD


@contextmanager
def count_selects(bind):
    """Count SELECT statements issued on a bind for the duration of the block."""
    counter = {"n": 0}

    def _before(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1

    event.listen(bind, "before_cursor_execute", _before)
    try:
        yield counter
    finally:
        event.remove(bind, "before_cursor_execute", _before)


def _make_lead(db, **overrides):
    payload_dict = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4())}
    payload_dict.update(overrides)
    payload = AngiLeadPayload.model_validate(payload_dict)
    receipt = WebhookReceipt(
        headers={}, raw_body=payload_dict, auth_valid=True,
        correlation_id=payload_dict["CorrelationId"],
    )
    db.add(receipt)
    db.flush()
    return process_lead(db, receipt, payload)


def test_get_recent_leads_no_nplus1(seeded_db):
    # Leads spanning BOTH seeded tenants — this mirrors the admin dashboard view.
    # Spanning tenants matters: the ORM identity map dedups tenant loads, so leads
    # that all share one tenant would mask the N+1 (one lazy load, then cached).
    for al_account_id in ("100001", "100002", "100001", "100002", "100001", "100002"):
        _make_lead(seeded_db, ALAccountId=al_account_id)
    seeded_db.flush()
    seeded_db.expire_all()  # force fresh loads inside the function under test

    with count_selects(seeded_db.get_bind()) as c:
        leads, total = get_recent_leads(seeded_db, limit=30)

    assert total == 6
    assert all(row["tenant_name"] for row in leads)  # tenant actually loaded
    # count() + a single joined SELECT for the page = 2. Without joinedload this
    # is 2 + one lazy lookup per distinct tenant (4 here, and one-per-lead in the
    # worst case where every lead is a different tenant).
    assert c["n"] <= 3, f"expected bounded query count, got {c['n']} (N+1?)"


def test_get_duplicate_pairs_no_nplus1(seeded_db):
    # 5 identical leads -> 4 duplicate matches (each new lead matches a prior one).
    for _ in range(5):
        _make_lead(seeded_db)
    seeded_db.flush()
    seeded_db.expire_all()

    with count_selects(seeded_db.get_bind()) as c:
        pairs = get_duplicate_pairs(seeded_db, limit=100)

    assert len(pairs) == 4
    # names/emails come off m.lead and m.original — confirm they loaded
    assert all(p["lead_name"].strip() and p["original_name"].strip() for p in pairs)
    # A single joined SELECT. Without joinedload this would be 1 + 2*4 (lead +
    # original per row).
    assert c["n"] <= 2, f"expected bounded query count, got {c['n']} (N+1?)"
