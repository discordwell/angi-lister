"""Tests for the ingestion service — full pipeline logic."""

import uuid

from app.models import Lead, WebhookReceipt, LeadEvent, OutboundMessage
from app.schemas.angi import AngiLeadPayload
from app.services.ingestion import process_lead
from tests.conftest import SAMPLE_LEAD


class TestProcessLead:
    def _make_receipt(self, db, payload_dict):
        receipt = WebhookReceipt(
            headers={}, raw_body=payload_dict, auth_valid=True,
            correlation_id=payload_dict.get("CorrelationId"),
        )
        db.add(receipt)
        db.flush()
        return receipt

    def test_mapped_lead_creates_outbound_message(self, seeded_db):
        payload_dict = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4())}
        payload = AngiLeadPayload.model_validate(payload_dict)
        receipt = self._make_receipt(seeded_db, payload_dict)

        lead = process_lead(seeded_db, receipt, payload)

        assert lead.status == "mapped"
        assert lead.tenant_id is not None
        msgs = seeded_db.query(OutboundMessage).filter(OutboundMessage.lead_id == lead.id).all()
        assert len(msgs) == 1
        assert msgs[0].status == "pending"
        assert msgs[0].recipient == "jane.doe@example.com"

    def test_unmapped_lead_skips_outbound(self, seeded_db):
        payload_dict = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4()), "ALAccountId": "UNKNOWN"}
        payload = AngiLeadPayload.model_validate(payload_dict)
        receipt = self._make_receipt(seeded_db, payload_dict)

        lead = process_lead(seeded_db, receipt, payload)

        assert lead.status == "unmapped"
        assert lead.tenant_id is None
        msgs = seeded_db.query(OutboundMessage).filter(OutboundMessage.lead_id == lead.id).all()
        assert len(msgs) == 0

    def test_idempotency_returns_existing(self, seeded_db):
        corr_id = str(uuid.uuid4())
        payload_dict = {**SAMPLE_LEAD, "CorrelationId": corr_id}
        payload = AngiLeadPayload.model_validate(payload_dict)

        receipt1 = self._make_receipt(seeded_db, payload_dict)
        lead1 = process_lead(seeded_db, receipt1, payload)

        receipt2 = self._make_receipt(seeded_db, payload_dict)
        lead2 = process_lead(seeded_db, receipt2, payload)

        assert lead1.id == lead2.id
        # Only one outbound message should exist
        msgs = seeded_db.query(OutboundMessage).filter(OutboundMessage.lead_id == lead1.id).all()
        assert len(msgs) == 1

    def test_events_emitted_for_mapped_lead(self, seeded_db):
        payload_dict = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4())}
        payload = AngiLeadPayload.model_validate(payload_dict)
        receipt = self._make_receipt(seeded_db, payload_dict)

        lead = process_lead(seeded_db, receipt, payload)

        events = seeded_db.query(LeadEvent).filter(LeadEvent.lead_id == lead.id).all()
        event_types = [e.event_type for e in events]
        assert "lead_created" in event_types
        assert "tenant_mapped" in event_types
        assert "email_queued" in event_types

    def test_simulated_flag_passed_to_outbound(self, seeded_db):
        payload_dict = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4())}
        payload = AngiLeadPayload.model_validate(payload_dict)
        receipt = self._make_receipt(seeded_db, payload_dict)

        lead = process_lead(seeded_db, receipt, payload, is_simulated=True)

        msg = seeded_db.query(OutboundMessage).filter(OutboundMessage.lead_id == lead.id).first()
        assert msg.is_simulated is True
