"""Duplicate lead detection service."""

import re
import logging

from sqlalchemy.orm import Session

from app.models import Lead, DuplicateMatch, LeadEvent

log = logging.getLogger(__name__)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _normalize_phone(phone: str) -> str:
    return re.sub(r"[^a-z0-9]", "", phone.strip().lower())


def _normalize_address(address: str) -> str:
    return address.strip().lower()


def compute_fingerprint(email: str, phone: str, address: str) -> str:
    """Deterministic fingerprint: normalized email|phone|address."""
    return f"{_normalize_email(email)}|{_normalize_phone(phone)}|{_normalize_address(address)}"


def check_duplicates(db: Session, lead: Lead) -> DuplicateMatch | None:
    """Check for duplicate leads within the same tenant by fingerprint components.

    Scoring: email_match (0.4) + phone_match (0.3) + address_match (0.3).
    Threshold: >= 0.4 to flag as duplicate.
    """
    if not lead.tenant_id:
        return None

    norm_email = _normalize_email(lead.email)
    norm_phone = _normalize_phone(lead.phone)
    norm_address = _normalize_address(
        f"{lead.address_line1 or ''} {lead.city or ''} {lead.state or ''} {lead.postal_code or ''}"
    )

    # Find existing leads for the same tenant, excluding this lead itself
    existing_leads = (
        db.query(Lead)
        .filter(
            Lead.tenant_id == lead.tenant_id,
            Lead.id != lead.id,
        )
        .all()
    )

    best_match: Lead | None = None
    best_score = 0.0
    best_evidence: dict = {}

    for existing in existing_leads:
        ex_email = _normalize_email(existing.email)
        ex_phone = _normalize_phone(existing.phone)
        ex_address = _normalize_address(
            f"{existing.address_line1 or ''} {existing.city or ''} {existing.state or ''} {existing.postal_code or ''}"
        )

        email_match = ex_email == norm_email
        phone_match = ex_phone == norm_phone
        address_match = ex_address == norm_address

        score = (0.4 if email_match else 0.0) + (0.3 if phone_match else 0.0) + (0.3 if address_match else 0.0)

        if score >= 0.4 and score > best_score:
            best_score = score
            best_match = existing
            best_evidence = {
                "email_match": email_match,
                "phone_match": phone_match,
                "address_match": address_match,
                "score": round(score, 2),
            }

    if best_match is None:
        return None

    dup = DuplicateMatch(
        lead_id=lead.id,
        original_id=best_match.id,
        score=best_score,
        evidence=best_evidence,
    )
    db.add(dup)

    event = LeadEvent(
        lead_id=lead.id,
        event_type="duplicate_detected",
        payload={
            "original_lead_id": best_match.id,
            "score": round(best_score, 2),
            "evidence": best_evidence,
        },
    )
    db.add(event)
    db.flush()

    log.info(
        "Duplicate detected: lead %s matches original %s (score=%.2f)",
        lead.id,
        best_match.id,
        best_score,
    )

    return dup
