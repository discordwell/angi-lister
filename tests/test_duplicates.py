"""Tests for duplicate detection — fingerprint and scoring."""

import uuid

from app.models import Lead, DuplicateMatch, WebhookReceipt
from app.schemas.angi import AngiLeadPayload
from app.services.duplicates import compute_fingerprint, check_duplicates
from app.services.ingestion import process_lead
from tests.conftest import SAMPLE_LEAD


class TestFingerprint:
    def test_normalize_email(self):
        fp1 = compute_fingerprint("Jane@Example.COM", "5551234567", "123 Main St")
        fp2 = compute_fingerprint("jane@example.com", "5551234567", "123 Main St")
        assert fp1 == fp2

    def test_normalize_phone(self):
        fp1 = compute_fingerprint("a@b.com", "(555) 123-4567", "addr")
        fp2 = compute_fingerprint("a@b.com", "5551234567", "addr")
        assert fp1 == fp2

    def test_different_emails_different_fingerprint(self):
        fp1 = compute_fingerprint("alice@example.com", "5551234567", "addr")
        fp2 = compute_fingerprint("bob@example.com", "5551234567", "addr")
        assert fp1 != fp2


class TestDuplicateDetection:
    def _create_lead(self, db, corr_id=None, **overrides):
        payload_dict = {**SAMPLE_LEAD, "CorrelationId": corr_id or str(uuid.uuid4())}
        payload_dict.update(overrides)
        payload = AngiLeadPayload.model_validate(payload_dict)
        receipt = WebhookReceipt(
            headers={}, raw_body=payload_dict, auth_valid=True,
            correlation_id=payload_dict["CorrelationId"],
        )
        db.add(receipt)
        db.flush()
        return process_lead(db, receipt, payload)

    def test_same_consumer_flagged_as_duplicate(self, seeded_db):
        lead1 = self._create_lead(seeded_db)
        lead2 = self._create_lead(seeded_db)

        # Second lead should have a duplicate match
        match = (
            seeded_db.query(DuplicateMatch)
            .filter(DuplicateMatch.lead_id == lead2.id)
            .first()
        )
        assert match is not None
        assert match.original_id == lead1.id
        assert match.score >= 0.4

    def test_different_consumer_not_flagged(self, seeded_db):
        lead1 = self._create_lead(seeded_db)
        lead2 = self._create_lead(
            seeded_db,
            Email="different@example.com",
            PhoneNumber="9999999999",
            PostalAddress={
                "AddressFirstLine": "999 Other Rd",
                "City": "Chicago", "State": "IL", "PostalCode": "60601",
            },
        )

        match = (
            seeded_db.query(DuplicateMatch)
            .filter(DuplicateMatch.lead_id == lead2.id)
            .first()
        )
        assert match is None

    def test_evidence_has_match_details(self, seeded_db):
        lead1 = self._create_lead(seeded_db)
        lead2 = self._create_lead(seeded_db)

        match = (
            seeded_db.query(DuplicateMatch)
            .filter(DuplicateMatch.lead_id == lead2.id)
            .first()
        )
        assert "email_match" in match.evidence
        assert "phone_match" in match.evidence
        assert "score" in match.evidence

    def test_email_only_match_scores_0_4(self, seeded_db):
        lead1 = self._create_lead(seeded_db)
        # Same email, different phone and address
        lead2 = self._create_lead(
            seeded_db,
            PhoneNumber="0000000000",
            PostalAddress={
                "AddressFirstLine": "999 Other Rd",
                "City": "Chicago", "State": "IL", "PostalCode": "60601",
            },
        )

        match = (
            seeded_db.query(DuplicateMatch)
            .filter(DuplicateMatch.lead_id == lead2.id)
            .first()
        )
        assert match is not None
        assert match.score == 0.4
        assert match.evidence["email_match"] is True
        assert match.evidence["phone_match"] is False
