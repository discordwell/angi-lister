"""Webhook endpoint for Angi lead ingestion."""

import logging

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_bypass_db
from app.models import WebhookReceipt, LeadEvent
from app.schemas.angi import AngiLeadPayload, EXPECTED_FIELDS, EXPECTED_ADDRESS_FIELDS
from app.schemas.api import WebhookResponse
from app.services.ingestion import process_lead

log = logging.getLogger(__name__)

router = APIRouter()


def _detect_drift(raw: dict) -> dict | None:
    """Compare incoming keys against expected schema fields.

    Returns a drift report dict if discrepancies are found, else None.
    """
    incoming_top = set(raw.keys())
    missing = EXPECTED_FIELDS - incoming_top
    extra = incoming_top - EXPECTED_FIELDS

    addr_drift: dict = {}
    if "PostalAddress" in raw and isinstance(raw["PostalAddress"], dict):
        incoming_addr = set(raw["PostalAddress"].keys())
        addr_missing = EXPECTED_ADDRESS_FIELDS - incoming_addr
        addr_extra = incoming_addr - EXPECTED_ADDRESS_FIELDS
        if addr_missing or addr_extra:
            addr_drift = {
                "missing": sorted(addr_missing) if addr_missing else [],
                "extra": sorted(addr_extra) if addr_extra else [],
            }

    if not missing and not extra and not addr_drift:
        return None

    drift: dict = {}
    if missing:
        drift["missing_fields"] = sorted(missing)
    if extra:
        drift["extra_fields"] = sorted(extra)
    if addr_drift:
        drift["address"] = addr_drift
    return drift


@router.post("/webhooks/angi/leads", response_model=WebhookResponse)
async def receive_angi_lead(
    request: Request,
    db: Session = Depends(get_bypass_db),
    x_api_key: str | None = Header(None),
):
    """Receive an Angi lead webhook.

    Auth: X-API-KEY header must match settings.angi_api_key.
    On auth failure, nothing is persisted and a 401 is returned.
    On parse failure, a 200 is returned (to suppress Angi retries) with the receipt id.
    """

    # ---- Auth ----------------------------------------------------------------
    if not x_api_key or x_api_key != settings.angi_api_key:
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})

    # ---- Read raw body -------------------------------------------------------
    raw_body: dict = await request.json()
    raw_headers = dict(request.headers)

    # ---- Persist receipt immediately -----------------------------------------
    receipt = WebhookReceipt(
        headers=raw_headers,
        raw_body=raw_body,
        auth_valid=True,
        correlation_id=raw_body.get("CorrelationId"),
    )
    db.add(receipt)
    db.flush()  # get receipt.id

    # ---- Parse / validate ----------------------------------------------------
    try:
        payload = AngiLeadPayload.model_validate(raw_body)
    except ValidationError as exc:
        receipt.parse_valid = False

        drift = _detect_drift(raw_body)
        if drift:
            receipt.schema_drift = drift

        db.add(LeadEvent(
            receipt_id=receipt.id,
            event_type="parse_failed",
            payload={
                "errors": exc.errors(),
                "schema_drift": drift,
            },
        ))

        resp = WebhookResponse(
            receipt_id=receipt.id,
            message="Parse failed; receipt recorded",
        )
        receipt.response_body = f"<success>receipt_id={receipt.id}</success>"
        db.commit()

        log.warning("Parse failure on receipt %s: %s", receipt.id, exc.error_count())

        # Lightweight alert check (debounced, non-fatal)
        try:
            from app.services.monitoring import check_and_alert_parse_failure
            check_and_alert_parse_failure(db)
        except Exception:
            log.exception("Alert check failed (non-fatal)")

        return resp

    # ---- Parse succeeded — ingest --------------------------------------------
    receipt.parse_valid = True

    lead = process_lead(db, receipt, payload)

    resp_body = (
        f"<success>receipt_id={receipt.id} "
        f"lead_id={lead.id} "
        f"correlation_id={lead.correlation_id}</success>"
    )
    receipt.response_body = resp_body
    db.commit()

    log.info("Lead ingested: receipt=%s lead=%s", receipt.id, lead.id)

    return WebhookResponse(
        receipt_id=receipt.id,
        lead_id=lead.id,
        correlation_id=lead.correlation_id,
        message=resp_body,
    )
