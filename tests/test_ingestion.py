"""Tests for the ingestion service — full pipeline logic."""

import uuid

from app.models import Lead, Tenant, WebhookReceipt, LeadEvent, OutboundMessage
from app.routers.api import api_replay_unmapped
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

    def test_email_queued_event_references_real_outbound_message_id(self, seeded_db):
        """The email_queued audit event must carry the actual outbound message id.

        msg.id is a flush-time default (``default=lambda: uuid4()``), so building
        the event payload before the message was flushed captured None. The later
        email_sent / email_failed events are built post-flush and DO carry the id,
        so the queued event was the odd one out — the audit trail could not tie
        the queued event to the message those terminal events identify.
        """
        payload_dict = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4())}
        payload = AngiLeadPayload.model_validate(payload_dict)
        receipt = self._make_receipt(seeded_db, payload_dict)

        lead = process_lead(seeded_db, receipt, payload)

        msg = seeded_db.query(OutboundMessage).filter(OutboundMessage.lead_id == lead.id).one()
        queued = (
            seeded_db.query(LeadEvent)
            .filter(LeadEvent.lead_id == lead.id, LeadEvent.event_type == "email_queued")
            .one()
        )
        assert queued.payload["outbound_message_id"] is not None
        assert queued.payload["outbound_message_id"] == msg.id


class TestReplayUnmapped:
    """The replay-unmapped endpoint queues outbound messages for leads that were
    unmapped when first received — it must uphold the same audit invariant."""

    def test_replay_records_real_outbound_message_id(self, seeded_db):
        # seeded_db maps al_account_id 100001 -> Hoffmann Brothers.
        tenant = seeded_db.query(Tenant).filter(Tenant.slug == "hoffmann-brothers").one()
        lead = Lead(
            correlation_id=str(uuid.uuid4()),
            al_account_id="100001",  # now mapped, but the lead arrived unmapped
            status="unmapped",
            first_name="Bob",
            last_name="Builder",
            email="bob.builder@example.com",
            phone="5554332646",
            raw_payload={},
        )
        seeded_db.add(lead)
        seeded_db.flush()

        result = api_replay_unmapped(tenant_id=tenant.id, db=seeded_db)
        assert result["replayed"] == 1

        msg = seeded_db.query(OutboundMessage).filter(OutboundMessage.lead_id == lead.id).one()
        queued = (
            seeded_db.query(LeadEvent)
            .filter(LeadEvent.lead_id == lead.id, LeadEvent.event_type == "email_queued")
            .one()
        )
        assert queued.payload["outbound_message_id"] is not None
        assert queued.payload["outbound_message_id"] == msg.id
