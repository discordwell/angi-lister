"""Tests for duplicate detection — fingerprint and scoring."""

import uuid

from app.models import DuplicateMatch, WebhookReceipt
from app.schemas.angi import AngiLeadPayload
from app.services.duplicates import compute_fingerprint
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
        self._create_lead(seeded_db)
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
        self._create_lead(seeded_db)
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
        self._create_lead(seeded_db)
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

    def test_blank_phone_and_address_not_matched(self, seeded_db):
        """Two unrelated leads (different emails) that both omit phone and
        address must NOT be flagged as duplicates. Before the fix, blank == blank
        scored phone_match (0.3) + address_match (0.3) = 0.6, a false positive
        that polluted the rebate-claim export."""
        blank_addr = {
            "AddressFirstLine": "", "AddressSecondLine": "",
            "City": "", "State": "", "PostalCode": "",
        }
        self._create_lead(
            seeded_db, Email="alice@example.com", PhoneNumber="", PostalAddress=blank_addr
        )
        lead2 = self._create_lead(
            seeded_db, Email="bob@example.com", PhoneNumber="", PostalAddress=blank_addr
        )

        match = (
            seeded_db.query(DuplicateMatch)
            .filter(DuplicateMatch.lead_id == lead2.id)
            .first()
        )
        assert match is None

    def test_blank_email_does_not_contribute_to_score(self, seeded_db):
        """A blank email shared by two leads must not count as an email match.
        Here phone and address are identical (0.3 + 0.3 = 0.6) so the pair is
        still flagged, but the score must be 0.6, not 1.0, and email_match
        must be False."""
        self._create_lead(seeded_db, Email="")
        lead2 = self._create_lead(seeded_db, Email="")

        match = (
            seeded_db.query(DuplicateMatch)
            .filter(DuplicateMatch.lead_id == lead2.id)
            .first()
        )
        assert match is not None
        assert match.score == 0.6
        assert match.evidence["email_match"] is False
        assert match.evidence["phone_match"] is True
        assert match.evidence["address_match"] is True
