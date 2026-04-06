"""Tests for the 3-pass email personalization engine."""

import datetime as dt
import uuid

import pytest
from sqlalchemy.orm import Session

from app.models import (
    Lead, LeadEvent, OutboundMessage, Tenant, AngiMapping,
    TenantHomeBase, TenantJobRule, TenantSpecial, GeocodeCache,
)
from app.services.personalization import (
    _check_repeat_customer,
    _check_job_rules,
    _compute_offers,
    _special_matches,
    personalize_outbound,
    PersonalizationContext,
    NearestBase,
    _build_system_prompt,
    _build_user_prompt,
)
from app.services.geo_utils import haversine_miles


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tenant(db: Session) -> Tenant:
    t = Tenant(
        name="Hormuz Home Heating",
        slug="hormuz-heating",
        brand_color="#d97706",
        phone="(317) 555-0199",
        email="service@hormuz.example.com",
        personalization_enabled=True,
        sample_email="Thanks for reaching out! We love keeping homes warm.",
        pricing_tiers=[
            {"max_mi": 1, "text": "$39 diagnostic"},
            {"max_mi": 5, "text": "$59 diagnostic"},
        ],
    )
    db.add(t)
    db.flush()
    db.add(AngiMapping(al_account_id="999001", tenant_id=t.id))
    db.flush()
    return t


@pytest.fixture
def lead(db: Session, tenant: Tenant) -> Lead:
    ld = Lead(
        correlation_id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        al_account_id="999001",
        status="mapped",
        first_name="Alice",
        last_name="Smith",
        email="alice@example.com",
        phone="5559991234",
        city="Indianapolis",
        state="IN",
        postal_code="46201",
        category="Indianapolis - Water Heater Replacement",
        description="Our water heater is leaking and needs replacement ASAP.",
        urgency="Today/Emergency",
        raw_payload={},
        fingerprint="alice@example.com|5559991234|indianapolis in 46201",
    )
    db.add(ld)
    db.flush()
    return ld


@pytest.fixture
def outbound_msg(db: Session, lead: Lead, tenant: Tenant) -> OutboundMessage:
    msg = OutboundMessage(
        lead_id=lead.id,
        tenant_id=tenant.id,
        recipient=lead.email,
        subject="placeholder",
        body_html="PLACEHOLDER",
        body_text="PLACEHOLDER",
        status="pending",
    )
    db.add(msg)
    db.flush()
    return msg


@pytest.fixture
def home_base(db: Session, tenant: Tenant) -> TenantHomeBase:
    hb = TenantHomeBase(
        tenant_id=tenant.id,
        name="Downtown Shop",
        address="100 Monument Circle, Indianapolis, IN 46204",
        lat=39.7684,
        lng=-86.1581,
    )
    db.add(hb)
    db.flush()
    return hb


@pytest.fixture
def job_rules(db: Session, tenant: Tenant) -> list[TenantJobRule]:
    rules = [
        TenantJobRule(tenant_id=tenant.id, category_pattern="Heater", rule_type="whitelist"),
        TenantJobRule(tenant_id=tenant.id, category_pattern="HVAC", rule_type="whitelist"),
        TenantJobRule(tenant_id=tenant.id, category_pattern="Water Heater", rule_type="wantlist"),
        TenantJobRule(tenant_id=tenant.id, category_pattern="Roofing", rule_type="blacklist"),
    ]
    db.add_all(rules)
    db.flush()
    return rules


@pytest.fixture
def special(db: Session, tenant: Tenant) -> TenantSpecial:
    sp = TenantSpecial(
        tenant_id=tenant.id,
        name="Water Heater Discount",
        description="$100 off any water heater replacement",
        discount_text="$100 off install",
        conditions={"category_contains": "Water Heater"},
    )
    db.add(sp)
    db.flush()
    return sp


# ── Haversine tests ──────────────────────────────────────────────────────────

class TestHaversine:
    def test_same_point_is_zero(self):
        assert haversine_miles(39.77, -86.16, 39.77, -86.16) == pytest.approx(0.0, abs=0.01)

    def test_known_distance(self):
        # Indianapolis to Chicago ~165 miles
        d = haversine_miles(39.7684, -86.1581, 41.8781, -87.6298)
        assert 160 < d < 175


# ── Pass 1: Repeat customer ──────────────────────────────────────────────────

class TestRepeatCustomer:
    def test_no_prior_leads(self, db, lead, tenant):
        result = _check_repeat_customer(db, lead, tenant)
        assert result == []

    def test_finds_prior_lead_same_email(self, db, lead, tenant):
        prior = Lead(
            correlation_id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            al_account_id="999001",
            status="mapped",
            first_name="Alice",
            last_name="Smith",
            email="alice@example.com",
            phone="5550000000",
            city="Indianapolis",
            state="IN",
            postal_code="46201",
            category="Indianapolis - Furnace Repair",
            description="Furnace making weird noises",
            urgency="This Week",
            raw_payload={},
            fingerprint="x",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
        )
        db.add(prior)
        db.flush()

        result = _check_repeat_customer(db, lead, tenant)
        assert len(result) == 1
        assert result[0].id == prior.id

    def test_ignores_old_leads(self, db, lead, tenant):
        old = Lead(
            correlation_id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            al_account_id="999001",
            status="mapped",
            first_name="Alice",
            last_name="Smith",
            email="alice@example.com",
            phone="5550000000",
            raw_payload={},
            fingerprint="x",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=10),
        )
        db.add(old)
        db.flush()

        result = _check_repeat_customer(db, lead, tenant)
        assert result == []


# ── Pass 2: Job rules ────────────────────────────────────────────────────────

class TestJobRules:
    def test_no_rules_means_send(self, db, lead, tenant):
        should_send, is_wantlisted, reason = _check_job_rules(db, lead, tenant)
        assert should_send is True
        assert is_wantlisted is False

    def test_blacklisted_category_declined(self, db, lead, tenant, job_rules):
        lead.category = "Indianapolis - Roofing Repair"
        db.flush()
        should_send, _, reason = _check_job_rules(db, lead, tenant)
        assert should_send is False
        assert "Blacklisted" in reason

    def test_whitelisted_category_passes(self, db, lead, tenant, job_rules):
        lead.category = "Indianapolis - HVAC Repair"
        db.flush()
        should_send, is_wantlisted, _ = _check_job_rules(db, lead, tenant)
        assert should_send is True

    def test_wantlisted_category_flagged(self, db, lead, tenant, job_rules):
        lead.category = "Indianapolis - Water Heater Replacement"
        db.flush()
        should_send, is_wantlisted, _ = _check_job_rules(db, lead, tenant)
        assert should_send is True
        assert is_wantlisted is True

    def test_wantlisted_category_bypasses_whitelist(self, db, lead, tenant):
        """A wantlisted category should be accepted even if it doesn't match any whitelist pattern."""
        rules = [
            TenantJobRule(tenant_id=tenant.id, category_pattern="HVAC", rule_type="whitelist"),
            TenantJobRule(tenant_id=tenant.id, category_pattern="Plumbing", rule_type="whitelist"),
            TenantJobRule(tenant_id=tenant.id, category_pattern="Water Heater", rule_type="wantlist"),
        ]
        db.add_all(rules)
        db.flush()

        lead.category = "St. Louis - Water Heater Repair"
        db.flush()
        should_send, is_wantlisted, reason = _check_job_rules(db, lead, tenant)
        assert should_send is True
        assert is_wantlisted is True

    def test_unlisted_category_declined_when_whitelist_exists(self, db, lead, tenant, job_rules):
        lead.category = "Indianapolis - Landscaping"
        db.flush()
        should_send, _, reason = _check_job_rules(db, lead, tenant)
        assert should_send is False
        assert "whitelist" in reason.lower()


# ── Pass 3: Offers ───────────────────────────────────────────────────────────

class TestOffers:
    def test_special_matches_category(self, lead, special):
        assert _special_matches(special, lead, distance_mi=2.0) is True

    def test_special_no_match_wrong_category(self, lead, special):
        lead.category = "Indianapolis - Landscaping"
        assert _special_matches(special, lead, distance_mi=2.0) is False

    def test_special_with_distance_condition(self, lead):
        sp = TenantSpecial(
            name="Close-by discount",
            discount_text="$20 off",
            conditions={"max_distance_mi": 3.0},
        )
        assert _special_matches(sp, lead, distance_mi=2.0) is True
        assert _special_matches(sp, lead, distance_mi=5.0) is False
        assert _special_matches(sp, lead, distance_mi=None) is False

    def test_compute_offers_with_geocode(self, db, lead, tenant, home_base, special, monkeypatch):
        # Mock geocoding to return a point ~0.5mi from home base
        monkeypatch.setattr(
            "app.services.personalization.geocode_address",
            lambda db, a, c, s, p: (39.770, -86.160),
        )
        nearest, pricing, best, others = _compute_offers(db, lead, tenant)
        assert nearest is not None
        assert nearest.name == "Downtown Shop"
        assert nearest.distance_mi < 1.0
        assert pricing == "$39 diagnostic"
        assert best is not None
        assert best.name == "Water Heater Discount"

    def test_compute_offers_no_geocode(self, db, lead, tenant, home_base, special, monkeypatch):
        monkeypatch.setattr(
            "app.services.personalization.geocode_address",
            lambda db, a, c, s, p: None,
        )
        nearest, pricing, best, others = _compute_offers(db, lead, tenant)
        assert nearest is None
        assert pricing is None
        # Special with category_contains still matches (no distance condition)
        assert best is not None


# ── Prompt building ──────────────────────────────────────────────────────────

class TestPromptBuilding:
    def test_system_prompt_includes_tenant_name(self, tenant):
        prompt = _build_system_prompt(tenant)
        assert "Hormuz Home Heating" in prompt

    def test_system_prompt_includes_sample_email(self, tenant):
        prompt = _build_system_prompt(tenant)
        assert "keeping homes warm" in prompt

    def test_user_prompt_includes_lead_details(self, lead, tenant):
        ctx = PersonalizationContext(lead=lead, tenant=tenant)
        prompt = _build_user_prompt(ctx)
        assert "Alice" in prompt
        assert "Water Heater" in prompt
        assert "leaking" in prompt

    def test_user_prompt_includes_offers(self, lead, tenant, special):
        ctx = PersonalizationContext(
            lead=lead, tenant=tenant,
            best_offer=special,
            nearest_base=NearestBase(name="Downtown Shop", distance_mi=0.5, lat=0, lng=0),
            pricing_tier="$39 diagnostic",
            is_wantlisted=True,
        )
        prompt = _build_user_prompt(ctx)
        assert "$100 off install" in prompt
        assert "Downtown Shop" in prompt
        assert "$39 diagnostic" in prompt
        assert "high-priority" in prompt


# ── Full pipeline ────────────────────────────────────────────────────────────

class TestPersonalizePipeline:
    def test_personalize_sends_email(self, db, outbound_msg, lead, tenant, monkeypatch):
        monkeypatch.setattr(
            "app.services.personalization.geocode_address",
            lambda db, a, c, s, p: None,
        )
        monkeypatch.setattr(
            "app.services.personalization.generate_email",
            lambda sys, usr, **kw: (
                "SEND",
                "We'd love to help with your water heater replacement!",
                1500,
            ),
        )
        result = personalize_outbound(db, outbound_msg)
        assert result is True
        assert outbound_msg.generation_method == "llm"
        assert outbound_msg.llm_duration_ms == 1500
        assert "water heater replacement" in outbound_msg.body_text

    def test_personalize_skips_repeat(self, db, outbound_msg, lead, tenant, monkeypatch):
        # Add a prior lead
        prior = Lead(
            correlation_id=str(uuid.uuid4()),
            tenant_id=tenant.id, al_account_id="999001", status="mapped",
            first_name="Alice", last_name="Smith",
            email="alice@example.com", phone="5550000000",
            raw_payload={}, fingerprint="x",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
        )
        db.add(prior)
        db.flush()

        monkeypatch.setattr(
            "app.services.personalization.geocode_address",
            lambda db, a, c, s, p: None,
        )
        monkeypatch.setattr(
            "app.services.personalization.generate_email",
            lambda sys, usr, **kw: ("SKIP", "", 800),
        )
        result = personalize_outbound(db, outbound_msg)
        assert result is False
        assert outbound_msg.status == "skipped"

    def test_personalize_declines_blacklisted(self, db, outbound_msg, lead, tenant, job_rules, monkeypatch):
        lead.category = "Indianapolis - Roofing Repair"
        db.flush()

        monkeypatch.setattr(
            "app.services.personalization.geocode_address",
            lambda db, a, c, s, p: None,
        )
        result = personalize_outbound(db, outbound_msg)
        assert result is False
        assert outbound_msg.status == "declined"
        assert outbound_msg.generation_method == "declined"

        events = db.query(LeadEvent).filter(
            LeadEvent.lead_id == lead.id, LeadEvent.event_type == "email_declined"
        ).all()
        assert len(events) == 1

    def test_llm_failure_raises(self, db, outbound_msg, lead, tenant, monkeypatch):
        """LLM error should propagate so email.py can catch and fallback."""
        from app.services.llm import LLMError

        monkeypatch.setattr(
            "app.services.personalization.geocode_address",
            lambda db, a, c, s, p: None,
        )
        monkeypatch.setattr(
            "app.services.personalization.generate_email",
            lambda sys, usr, **kw: (_ for _ in ()).throw(LLMError("API down")),
        )
        with pytest.raises(LLMError):
            personalize_outbound(db, outbound_msg)
