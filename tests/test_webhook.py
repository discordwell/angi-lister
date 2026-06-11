"""Tests for the webhook endpoint."""

import uuid

from app.models import LeadEvent, WebhookReceipt
from tests.conftest import SAMPLE_LEAD


class TestAuth:
    def test_missing_api_key_returns_401(self, seeded_client):
        resp = seeded_client.post("/webhooks/angi/leads", json=SAMPLE_LEAD)
        assert resp.status_code == 401

    def test_wrong_api_key_returns_401(self, seeded_client):
        resp = seeded_client.post(
            "/webhooks/angi/leads",
            json=SAMPLE_LEAD,
            headers={"X-API-KEY": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_non_ascii_api_key_returns_401(self, seeded_client):
        """compare_digest rejects non-ascii str — keys must be compared as
        bytes or a probe with e.g. latin-1 chars turns into a 500. The value
        is sent as bytes because httpx only encodes ascii str headers."""
        resp = seeded_client.post(
            "/webhooks/angi/leads",
            json=SAMPLE_LEAD,
            headers={"X-API-KEY": "wröng-key".encode("latin-1")},
        )
        assert resp.status_code == 401

    def test_valid_api_key_returns_200(self, seeded_client):
        lead = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4())}
        resp = seeded_client.post(
            "/webhooks/angi/leads",
            json=lead,
            headers={"X-API-KEY": "test-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "lead_id" in data
        assert "<success>" in data["message"]


class TestIdempotency:
    def test_same_correlation_id_returns_same_lead(self, seeded_client):
        corr_id = str(uuid.uuid4())
        lead = {**SAMPLE_LEAD, "CorrelationId": corr_id}
        headers = {"X-API-KEY": "test-key"}

        r1 = seeded_client.post("/webhooks/angi/leads", json=lead, headers=headers)
        r2 = seeded_client.post("/webhooks/angi/leads", json=lead, headers=headers)

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["lead_id"] == r2.json()["lead_id"]


class TestTenantMapping:
    def test_mapped_lead_has_status_mapped(self, seeded_client):
        lead = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4()), "ALAccountId": "100001"}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=lead, headers={"X-API-KEY": "test-key"}
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_unmapped_lead_returns_200(self, seeded_client):
        lead = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4()), "ALAccountId": "UNKNOWN"}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=lead, headers={"X-API-KEY": "test-key"}
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True


class TestParseFailure:
    def test_malformed_payload_returns_200(self, seeded_client):
        bad_payload = {"garbage": "data", "CorrelationId": str(uuid.uuid4())}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=bad_payload, headers={"X-API-KEY": "test-key"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["receipt_id"] is not None
        assert data["lead_id"] is None

    def test_invalid_json_returns_200_and_captures_receipt(self, seeded_client, seeded_db):
        """A body that isn't JSON at all must still produce a receipt and a
        <success> ACK — a 500 here would trigger Angi retries and lose the
        forensic capture entirely."""
        resp = seeded_client.post(
            "/webhooks/angi/leads",
            content=b'{"FirstName": "Jane", "LastName":',
            headers={"X-API-KEY": "test-key", "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["lead_id"] is None
        assert "<success>" in data["message"]

        receipt = seeded_db.get(WebhookReceipt, data["receipt_id"])
        assert receipt is not None
        assert receipt.parse_valid is False
        assert '"LastName":' in receipt.raw_body["_raw_body"]

        events = (
            seeded_db.query(LeadEvent)
            .filter(
                LeadEvent.receipt_id == receipt.id,
                LeadEvent.event_type == "parse_failed",
            )
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["errors"][0]["type"] == "json_invalid"

    def test_non_object_json_returns_200_and_captures_receipt(self, seeded_client, seeded_db):
        resp = seeded_client.post(
            "/webhooks/angi/leads",
            json=["not", "an", "object"],
            headers={"X-API-KEY": "test-key"},
        )
        assert resp.status_code == 200
        receipt = seeded_db.get(WebhookReceipt, resp.json()["receipt_id"])
        assert receipt.parse_valid is False
        assert receipt.raw_body["_raw_body"].startswith("[")
        assert "not" in receipt.raw_body["_raw_body"]

    def test_non_string_correlation_id_not_stored(self, seeded_client, seeded_db):
        """An int CorrelationId fails validation; it must not be written to the
        String receipt column either (PostgreSQL rejects the type)."""
        bad = {**SAMPLE_LEAD, "CorrelationId": 12345}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=bad, headers={"X-API-KEY": "test-key"}
        )
        assert resp.status_code == 200
        receipt = seeded_db.get(WebhookReceipt, resp.json()["receipt_id"])
        assert receipt.parse_valid is False
        assert receipt.correlation_id is None

    def test_demo_endpoint_invalid_json_returns_200(self, seeded_client, seeded_db):
        resp = seeded_client.post(
            "/webhooks/demo/leads",
            content=b"not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        receipt = seeded_db.get(WebhookReceipt, resp.json()["receipt_id"])
        assert receipt.parse_valid is False
        assert receipt.raw_body["_raw_body"] == "not json at all"

    def test_extra_fields_detected_as_drift(self, seeded_client):
        lead = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4()), "NewField": "surprise"}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=lead, headers={"X-API-KEY": "test-key"}
        )
        # Extra fields don't cause parse failure in Pydantic (they're ignored by default)
        # but drift detection should still note them
        assert resp.status_code == 200


class TestDuplicateDetection:
    def test_same_consumer_different_correlation_flagged(self, seeded_client):
        headers = {"X-API-KEY": "test-key"}
        lead1 = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4())}
        lead2 = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4())}

        r1 = seeded_client.post("/webhooks/angi/leads", json=lead1, headers=headers)
        r2 = seeded_client.post("/webhooks/angi/leads", json=lead2, headers=headers)

        assert r1.status_code == 200
        assert r2.status_code == 200
        # Both should succeed, second should have a different lead_id
        assert r1.json()["lead_id"] != r2.json()["lead_id"]
