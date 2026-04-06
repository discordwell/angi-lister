"""Tests for the email worker — message processing and delivery."""

import uuid

from app.models import Lead, OutboundMessage, LeadEvent, WebhookReceipt
from app.schemas.angi import AngiLeadPayload
from app.services.email import process_outbound_message, populate_outbound
from app.services.ingestion import process_lead
from tests.conftest import SAMPLE_LEAD


class TestPopulateOutbound:
    def _create_lead_with_message(self, db):
        payload_dict = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4())}
        payload = AngiLeadPayload.model_validate(payload_dict)
        receipt = WebhookReceipt(
            headers={}, raw_body=payload_dict, auth_valid=True,
            correlation_id=payload_dict["CorrelationId"],
        )
        db.add(receipt)
        db.flush()
        lead = process_lead(db, receipt, payload)
        msg = db.query(OutboundMessage).filter(OutboundMessage.lead_id == lead.id).first()
        return lead, msg

    def test_placeholder_gets_rendered(self, seeded_db):
        lead, msg = self._create_lead_with_message(seeded_db)
        assert msg.body_html == "PLACEHOLDER"

        populate_outbound(seeded_db, msg)

        assert msg.body_html != "PLACEHOLDER"
        assert "Apex HVAC" in msg.body_html
        assert lead.first_name in msg.body_html

    def test_already_rendered_not_overwritten(self, seeded_db):
        lead, msg = self._create_lead_with_message(seeded_db)
        msg.body_html = "<p>Custom content</p>"
        msg.body_text = "Custom content"
        seeded_db.flush()

        populate_outbound(seeded_db, msg)

        assert msg.body_html == "<p>Custom content</p>"


class TestProcessOutboundMessage:
    def _create_lead_with_message(self, db):
        payload_dict = {**SAMPLE_LEAD, "CorrelationId": str(uuid.uuid4())}
        payload = AngiLeadPayload.model_validate(payload_dict)
        receipt = WebhookReceipt(
            headers={}, raw_body=payload_dict, auth_valid=True,
            correlation_id=payload_dict["CorrelationId"],
        )
        db.add(receipt)
        db.flush()
        lead = process_lead(db, receipt, payload)
        msg = db.query(OutboundMessage).filter(OutboundMessage.lead_id == lead.id).first()
        return lead, msg

    def test_simulated_send_marks_sent(self, seeded_db):
        """With empty RESEND_API_KEY, messages are simulated and marked sent."""
        lead, msg = self._create_lead_with_message(seeded_db)

        success = process_outbound_message(seeded_db, msg)

        assert success is True
        assert msg.status == "sent"
        assert msg.provider_id == "simulated"
        assert msg.sent_at is not None

    def test_email_sent_event_emitted(self, seeded_db):
        lead, msg = self._create_lead_with_message(seeded_db)

        process_outbound_message(seeded_db, msg)

        events = (
            seeded_db.query(LeadEvent)
            .filter(LeadEvent.lead_id == lead.id, LeadEvent.event_type == "email_sent")
            .all()
        )
        assert len(events) == 1

    def test_simulated_flag_prevents_real_send(self, seeded_db):
        lead, msg = self._create_lead_with_message(seeded_db)
        msg.is_simulated = True
        seeded_db.flush()

        success = process_outbound_message(seeded_db, msg)

        assert success is True
        assert msg.provider_id == "simulated"
