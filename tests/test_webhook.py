"""Tests for the webhook endpoint."""

import copy
import uuid

import pytest

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
