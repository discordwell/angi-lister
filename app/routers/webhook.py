"""Webhook endpoint for Angi lead ingestion."""

import hmac
import json
import logging
import uuid

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

# Cap stored text for unparseable bodies — enough for forensics without letting
# a malformed megabyte-scale POST bloat the receipts table.
MAX_CAPTURED_BODY_CHARS = 10_000


def _api_key_valid(provided: str | None) -> bool:
    """Constant-time API key check (same primitive as cookie signing)."""
    if not provided or not settings.angi_api_key:
        return False
    return hmac.compare_digest(provided.encode(), settings.angi_api_key.encode())


async def _capture_body(request: Request) -> tuple[dict, list[dict] | None]:
    """Read the request body into a dict suitable for receipt capture.

    Receipts are first-class records: an authenticated POST must be persisted
    even when the body is not valid JSON, or not a JSON object at all. In those
    cases the literal body text is preserved under "_raw_body" and the second
    element describes the problem (same shape as pydantic's errors()).
    """
    text = (await request.body()).decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        wrapped = {"_raw_body": text[:MAX_CAPTURED_BODY_CHARS]}
        return wrapped, [{"type": "json_invalid", "msg": str(exc)}]
    if not isinstance(parsed, dict):
        wrapped = {"_raw_body": text[:MAX_CAPTURED_BODY_CHARS]}
        return wrapped, [{
            "type": "json_not_object",
            "msg": f"expected a JSON object, got {type(parsed).__name__}",
        }]
    return parsed, None


def _correlation_id(raw_body: dict) -> str | None:
    """CorrelationId for the receipt column; a non-string value won't fit a
    String column on PostgreSQL, so anything else is treated as absent."""
    cid = raw_body.get("CorrelationId")
    return cid if isinstance(cid, str) else None


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


def _record_parse_failure(
    db: Session, receipt: WebhookReceipt, errors: list[dict], drift: dict | None,
) -> WebhookResponse:
    """Mark the receipt failed, emit the parse_failed event, and commit.

    MUST return a 200 whose body contains the <success> pattern — Angi retries
    any response without it, and retrying an unparseable payload cannot help.
    """
    receipt.parse_valid = False
    if drift:
        receipt.schema_drift = drift
    db.add(LeadEvent(
        receipt_id=receipt.id,
        event_type="parse_failed",
        payload={"errors": errors, "schema_drift": drift},
    ))
    success_body = f"<success>receipt_id={receipt.id}</success>"
    receipt.response_body = success_body
    resp = WebhookResponse(receipt_id=receipt.id, message=success_body)
    db.commit()
    return resp


def _check_parse_failure_alert(db: Session) -> None:
    """Debounced error-rate alert check — never lets monitoring break the ACK."""
    try:
        from app.services.monitoring import check_and_alert_parse_failure
        check_and_alert_parse_failure(db)
    except Exception:
        log.exception("Alert check failed (non-fatal)")


@router.post("/webhooks/angi/leads", response_model=WebhookResponse)
async def receive_angi_lead(
    request: Request,
    db: Session = Depends(get_bypass_db),
    x_api_key: str | None = Header(None),
):
    """Receive an Angi lead webhook.

    Auth: X-API-KEY header must match settings.angi_api_key.
    On auth failure, nothing is persisted and a 401 is returned.
    On any parse failure — invalid JSON, a non-object body, or a schema
    mismatch — the raw body is still captured as a receipt and a 200 is
    returned (to suppress Angi retries) with the receipt id.
    """

    # ---- Auth ----------------------------------------------------------------
    if not _api_key_valid(x_api_key):
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})

    # ---- Read raw body -------------------------------------------------------
    raw_body, body_errors = await _capture_body(request)
    raw_headers = dict(request.headers)

    # ---- Persist receipt immediately -----------------------------------------
    receipt = WebhookReceipt(
        headers=raw_headers,
        raw_body=raw_body,
        auth_valid=True,
        correlation_id=_correlation_id(raw_body),
    )
    db.add(receipt)
    db.flush()  # get receipt.id

    # ---- Parse / validate ----------------------------------------------------
    if body_errors is not None:
        resp = _record_parse_failure(db, receipt, body_errors, drift=None)
        log.warning("Unparseable body on receipt %s: %s", resp.receipt_id, body_errors[0]["msg"])
        _check_parse_failure_alert(db)
        return resp

    try:
        payload = AngiLeadPayload.model_validate(raw_body)
    except ValidationError as exc:
        resp = _record_parse_failure(db, receipt, exc.errors(), _detect_drift(raw_body))
        log.warning("Parse failure on receipt %s: %s errors", resp.receipt_id, exc.error_count())
        _check_parse_failure_alert(db)
        return resp

    # ---- Parse succeeded — ingest --------------------------------------------
    receipt.parse_valid = True

    lead = process_lead(db, receipt, payload)

    # Capture IDs before commit — after commit, SQLAlchemy expires objects and
    # the refresh SELECT can fail under RLS if the connection pool resets context.
    receipt_id = receipt.id
    lead_id = lead.id
    correlation_id = lead.correlation_id

    resp_body = (
        f"<success>receipt_id={receipt_id} "
        f"lead_id={lead_id} "
        f"correlation_id={correlation_id}</success>"
    )
    receipt.response_body = resp_body
    db.commit()

    log.info("Lead ingested: receipt=%s lead=%s", receipt_id, lead_id)

    return WebhookResponse(
        receipt_id=receipt_id,
        lead_id=lead_id,
        correlation_id=correlation_id,
        message=resp_body,
    )


@router.post("/webhooks/demo/leads", response_model=WebhookResponse)
async def receive_demo_lead(
    request: Request,
    db: Session = Depends(get_bypass_db),
):
    """Demo endpoint — no API key required.

    Accepts the same Angi payload format. If ALAccountId is missing or
    unrecognized, defaults to the demo tenant (Paschal Air).
    """
    raw_body, body_errors = await _capture_body(request)
    raw_headers = dict(request.headers)

    if body_errors is None:
        # Default ALAccountId to demo tenant if not provided
        if not raw_body.get("ALAccountId"):
            raw_body["ALAccountId"] = settings.demo_al_account_id
        # Auto-generate CorrelationId if missing
        if not raw_body.get("CorrelationId"):
            raw_body["CorrelationId"] = str(uuid.uuid4())

    receipt = WebhookReceipt(
        headers=raw_headers,
        raw_body=raw_body,
        auth_valid=True,
        correlation_id=_correlation_id(raw_body),
    )
    db.add(receipt)
    db.flush()

    if body_errors is not None:
        return _record_parse_failure(db, receipt, body_errors, drift=None)

    try:
        payload = AngiLeadPayload.model_validate(raw_body)
    except ValidationError as exc:
        return _record_parse_failure(db, receipt, exc.errors(), _detect_drift(raw_body))

    receipt.parse_valid = True
    lead = process_lead(db, receipt, payload)

    # Capture IDs before commit — same RLS-refresh hazard as the Angi endpoint.
    receipt_id = receipt.id
    lead_id = lead.id
    correlation_id = lead.correlation_id

    resp_body = (
        f"<success>receipt_id={receipt_id} "
        f"lead_id={lead_id} "
        f"correlation_id={correlation_id}</success>"
    )
    receipt.response_body = resp_body
    db.commit()

    log.info("Demo lead ingested: receipt=%s lead=%s", receipt_id, lead_id)
    return WebhookResponse(
        receipt_id=receipt_id,
        lead_id=lead_id,
        correlation_id=correlation_id,
        message=resp_body,
    )
