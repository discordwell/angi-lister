"""Response schemas for API endpoints."""

from datetime import datetime

from pydantic import BaseModel


class WebhookResponse(BaseModel):
    success: bool = True
    receipt_id: str
    lead_id: str | None = None
    correlation_id: str | None = None
    message: str


class HealthResponse(BaseModel):
    status: str
    version: str = "0.1.0"


class ReadyResponse(BaseModel):
    status: str
    db: str
    worker: str


class LeadSummary(BaseModel):
    id: str
    correlation_id: str
    tenant_name: str | None
    first_name: str
    last_name: str
    email: str
    category: str | None
    urgency: str | None
    status: str
    created_at: datetime


class LeadDetail(LeadSummary):
    phone: str
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    source: str | None
    description: str | None
    raw_payload: dict
    events: list[dict]
    outbound_messages: list[dict]


class MetricsSummary(BaseModel):
    total_leads_24h: int
    total_leads_all: int
    median_speed_to_lead_seconds: float | None
    delivery_success_rate: float | None
    duplicate_rate: float | None
    unmapped_count: int
    parse_failure_count: int


class DuplicatePair(BaseModel):
    lead_id: str
    original_id: str
    score: float
    evidence: dict
    lead_name: str
    original_name: str
    lead_email: str
    created_at: datetime
