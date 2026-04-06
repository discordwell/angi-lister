"""Response schemas for API endpoints."""

from datetime import datetime
from typing import Literal

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
    conversion_rate: float | None = None


class OutcomeRequest(BaseModel):
    outcome: Literal["booked", "won", "lost"]
    notes: str | None = None


class SchemaHealthResponse(BaseModel):
    status: str
    schema_drift: dict | None = None
    error_rate: dict | None = None


class DuplicatePair(BaseModel):
    lead_id: str
    original_id: str
    score: float
    evidence: dict
    lead_name: str
    original_name: str
    lead_email: str
    created_at: datetime


# ── Tenant API schemas ───────────────────────────────────────────────────────

class TenantProfile(BaseModel):
    id: str
    name: str
    slug: str
    email: str | None
    phone: str | None
    brand_color: str
    timezone: str
    personalization_enabled: bool
    created_at: datetime


class HomeBaseIn(BaseModel):
    name: str
    address: str | None = None
    lat: float
    lng: float


class HomeBaseOut(HomeBaseIn):
    id: str
    created_at: datetime


class JobRuleIn(BaseModel):
    category_pattern: str
    rule_type: str


class JobRuleOut(JobRuleIn):
    id: str
    created_at: datetime


class SpecialIn(BaseModel):
    name: str
    description: str | None = None
    discount_text: str
    conditions: dict
    active: bool = True


class SpecialOut(SpecialIn):
    id: str
    created_at: datetime


class SpecialUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    discount_text: str | None = None
    conditions: dict | None = None
    active: bool | None = None


class TenantConfig(BaseModel):
    sample_email: str | None
    pricing_tiers: list[dict] | None
    personalization_enabled: bool
    intro_template: str | None
    llm_system_prompt: str | None
    brand_color: str
    email_from_name: str | None
    home_bases: list[HomeBaseOut]
    job_rules: list[JobRuleOut]
    specials: list[SpecialOut]


class TenantConfigUpdate(BaseModel):
    sample_email: str | None = None
    pricing_tiers: list[dict] | None = None
    personalization_enabled: bool | None = None
    intro_template: str | None = None
    llm_system_prompt: str | None = None
    brand_color: str | None = None
    email_from_name: str | None = None


class TenantCreate(BaseModel):
    name: str
    slug: str
    email: str | None = None
    phone: str | None = None
    brand_color: str = "#2563eb"
    timezone: str = "America/New_York"


class TenantUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    brand_color: str | None = None
    timezone: str | None = None
    email_from_name: str | None = None


class ApiKeyOut(BaseModel):
    id: str
    name: str
    key_prefix: str
    is_admin: bool
    last_used_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    raw_key: str


class ApiKeyCreate(BaseModel):
    name: str
    is_admin: bool = False


class MappingIn(BaseModel):
    al_account_id: str


class MappingOut(BaseModel):
    id: str
    al_account_id: str
    tenant_id: str
