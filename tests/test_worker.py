"""Tests for the email worker — message processing and delivery."""

import uuid

import pytest

from app import worker as worker_module
from app.db.session import SessionLocal, engine
from app.models import Base, OutboundMessage, LeadEvent, Tenant, AngiMapping, WebhookReceipt
from app.schemas.angi import AngiLeadPayload
from app.services.email import MAX_ATTEMPTS, process_outbound_message, populate_outbound
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
        assert "Hoffmann Brothers" in msg.body_html
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


@pytest.fixture
def worker_db():
    """Session on the real (file-backed) engine, like test_session_hygiene.

    run_cycle commits and rolls back as part of its contract; the standard
    savepoint-wrapped `db` fixture cannot survive that (its rollback discards
    the whole fixture transaction), so these tests need genuine transaction
    semantics. Seeds the tenant + mapping SAMPLE_LEAD expects.
    """
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        tenant = Tenant(
            name="Hoffmann Brothers", slug="hoffmann-brothers",
            brand_color="#1e3a5f", phone="(314) 555-0101",
            email="service@hoffmannbros.example.com", email_from_name="Hoffmann Brothers",
            timezone="America/Chicago",
        )
        db.add(tenant)
        db.flush()
        db.add(AngiMapping(al_account_id="100001", tenant_id=tenant.id))
        db.commit()
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


class TestRunCycleCrashHandling:
    """run_cycle's rollback also discards the attempts increment, so crashed
    attempts must be re-counted afterwards — otherwise a message that keeps
    raising is retried every poll cycle forever."""

    def _create_message(self, db) -> str:
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
        msg_id = msg.id
        db.commit()  # run_cycle rolls back on crash — the setup must be durable
        return msg_id

    @staticmethod
    def _boom(db, msg):
        raise RuntimeError("template exploded")

    def test_crash_counts_attempt_and_stays_pending(self, worker_db, monkeypatch):
        msg_id = self._create_message(worker_db)
        monkeypatch.setattr(worker_module, "process_outbound_message", self._boom)

        processed = worker_module.run_cycle(worker_db)

        assert processed == 0
        msg = worker_db.get(OutboundMessage, msg_id)
        assert msg.status == "pending"
        assert msg.attempts == 1
        assert "template exploded" in msg.last_error

    def test_repeated_crashes_eventually_fail_message(self, worker_db, monkeypatch):
        msg_id = self._create_message(worker_db)
        monkeypatch.setattr(worker_module, "process_outbound_message", self._boom)

        for _ in range(MAX_ATTEMPTS):
            worker_module.run_cycle(worker_db)

        msg = worker_db.get(OutboundMessage, msg_id)
        assert msg.status == "failed"
        assert msg.attempts == MAX_ATTEMPTS

        event = (
            worker_db.query(LeadEvent)
            .filter(
                LeadEvent.lead_id == msg.lead_id,
                LeadEvent.event_type == "email_failed",
            )
            .one()
        )
        assert event.payload["attempts"] == MAX_ATTEMPTS

        # Failed messages are no longer picked up
        assert worker_module.run_cycle(worker_db) == 0

    def test_crash_then_recovery_sends_normally(self, worker_db):
        """A transient crash must not block the message once processing works
        again — the next cycle picks it up and sends (simulated)."""
        msg_id = self._create_message(worker_db)

        worker_module._record_crashed_attempt(
            worker_db, msg_id, RuntimeError("transient"),
        )
        msg = worker_db.get(OutboundMessage, msg_id)
        assert msg.status == "pending"
        assert msg.attempts == 1

        processed = worker_module.run_cycle(worker_db)

        assert processed == 1
        msg = worker_db.get(OutboundMessage, msg_id)
        assert msg.status == "sent"
        assert msg.provider_id == "simulated"
