"""Lead ingestion service — maps, persists, and queues outbound for incoming Angi leads."""

import logging

from sqlalchemy.orm import Session

from app.models import Lead, WebhookReceipt, LeadEvent, OutboundMessage, AngiMapping
from app.schemas.angi import AngiLeadPayload
from app.services.duplicates import compute_fingerprint, check_duplicates

log = logging.getLogger(__name__)


def process_lead(
    db: Session,
    receipt: WebhookReceipt,
    payload: AngiLeadPayload,
    is_simulated: bool = False,
) -> Lead:
    """Ingest a validated Angi lead payload into the database.

    Steps:
      1. Idempotency check on CorrelationId
      2. Compute fingerprint
      3. Map ALAccountId -> tenant
      4. Create Lead + events
      5. Check for duplicates
      6. Create OutboundMessage row
    """

    # --- 1. Idempotency -----------------------------------------------------------
    existing = db.query(Lead).filter(Lead.correlation_id == payload.CorrelationId).first()
    if existing:
        log.info("Idempotent hit: correlation_id=%s already exists as lead %s", payload.CorrelationId, existing.id)
        return existing

    # --- 2. Fingerprint -----------------------------------------------------------
    address_str = (
        f"{payload.PostalAddress.AddressFirstLine} "
        f"{payload.PostalAddress.City} "
        f"{payload.PostalAddress.State} "
        f"{payload.PostalAddress.PostalCode}"
    )
    fingerprint = compute_fingerprint(payload.Email, payload.PhoneNumber, address_str)

    # --- 3. Tenant mapping --------------------------------------------------------
    mapping = (
        db.query(AngiMapping)
        .filter(AngiMapping.al_account_id == payload.ALAccountId)
        .first()
    )

    # --- 4. Create Lead -----------------------------------------------------------
    lead = Lead(
        correlation_id=payload.CorrelationId,
        receipt_id=receipt.id,
        al_account_id=payload.ALAccountId,
        tenant_id=mapping.tenant_id if mapping else None,
        status="mapped" if mapping else "unmapped",
        first_name=payload.FirstName,
        last_name=payload.LastName,
        email=payload.Email,
        phone=payload.PhoneNumber,
        address_line1=payload.PostalAddress.AddressFirstLine or None,
        address_line2=payload.PostalAddress.AddressSecondLine or None,
        city=payload.PostalAddress.City or None,
        state=payload.PostalAddress.State or None,
        postal_code=payload.PostalAddress.PostalCode or None,
        source=payload.Source or None,
        description=payload.Description or None,
        category=payload.Category or None,
        urgency=payload.Urgency or None,
        raw_payload=payload.model_dump(),
        fingerprint=fingerprint,
    )
    db.add(lead)
    db.flush()  # get lead.id

    # Update receipt with tenant_id now that we know it
    if mapping:
        receipt.tenant_id = mapping.tenant_id

    if not mapping:
        # Unmapped path — emit event, return early (no outbound message)
        db.add(LeadEvent(
            lead_id=lead.id,
            receipt_id=receipt.id,
            event_type="unmapped",
            payload={"al_account_id": payload.ALAccountId},
        ))
        db.flush()
        log.warning("No tenant mapping for ALAccountId=%s — lead %s marked unmapped", payload.ALAccountId, lead.id)
        return lead

    # Mapped path — emit creation events
    tenant = mapping.tenant
    db.add(LeadEvent(
        lead_id=lead.id,
        receipt_id=receipt.id,
        tenant_id=tenant.id,
        event_type="lead_created",
        payload={"correlation_id": payload.CorrelationId},
    ))
    db.add(LeadEvent(
        lead_id=lead.id,
        receipt_id=receipt.id,
        tenant_id=tenant.id,
        event_type="tenant_mapped",
        payload={"tenant_id": tenant.id, "tenant_name": tenant.name},
    ))
    db.flush()

    # --- 5. Duplicate check -------------------------------------------------------
    check_duplicates(db, lead)

    # --- 6. Outbound message (placeholder for Agent B) ----------------------------
    msg = OutboundMessage(
        lead_id=lead.id,
        tenant_id=tenant.id,
        channel="email",
        recipient=lead.email,
        subject=f"{tenant.name} — ready to help with {lead.category or 'your project'}!",
        body_html="PLACEHOLDER",
        body_text="PLACEHOLDER",
        status="pending",
        is_simulated=is_simulated,
    )
    db.add(msg)

    db.add(LeadEvent(
        lead_id=lead.id,
        receipt_id=receipt.id,
        tenant_id=tenant.id,
        event_type="email_queued",
        payload={"outbound_message_id": msg.id, "is_simulated": is_simulated},
    ))
    db.flush()

    log.info(
        "Lead %s created (tenant=%s, fingerprint=%s, simulated=%s)",
        lead.id, tenant.name, fingerprint, is_simulated,
    )
    return lead
