"""3-pass email personalization engine.

Pass 1: Repeat customer check (DB query, feeds context to LLM)
Pass 2: Job desirability — blacklist/whitelist/wantlist (deterministic)
Pass 3: Offer selection — geocode, proximity pricing, specials (deterministic)

Then a single LLM call (GPT-5.4) generates the email body and decides SEND/SKIP.
"""

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Lead, LeadEvent, OutboundMessage, Tenant, TenantJobRule, TenantSpecial, TenantFile
from app.models.tenant_home_base import TenantHomeBase
from app.services.email import populate_outbound
from app.services.geo_utils import haversine_miles
from app.services.geocoding import geocode_address
from app.services.llm import LLMError, generate_email

log = logging.getLogger(__name__)


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class NearestBase:
    name: str
    distance_mi: float
    lat: float
    lng: float


@dataclass
class PersonalizationContext:
    lead: Lead
    tenant: Tenant
    prior_leads: list[Lead] = field(default_factory=list)
    is_wantlisted: bool = False
    matched_job_rule: str | None = None
    lead_lat: float | None = None
    lead_lng: float | None = None
    nearest_base: NearestBase | None = None
    pricing_tier: str | None = None
    best_offer: TenantSpecial | None = None
    other_offers: list[TenantSpecial] = field(default_factory=list)


# ── Pass 1: Repeat customer check ────────────────────────────────────────────

def _check_repeat_customer(db: Session, lead: Lead, tenant: Tenant) -> list[Lead]:
    """Find leads with same email OR phone from this tenant in the last 7 days."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
    return (
        db.query(Lead)
        .filter(
            Lead.tenant_id == tenant.id,
            Lead.id != lead.id,
            Lead.created_at >= cutoff,
            or_(Lead.email == lead.email, Lead.phone == lead.phone),
        )
        .order_by(Lead.created_at.desc())
        .limit(5)
        .all()
    )


# ── Pass 2: Job desirability ─────────────────────────────────────────────────

def _check_job_rules(
    db: Session, lead: Lead, tenant: Tenant
) -> tuple[bool, bool, str | None]:
    """Returns (should_send, is_wantlisted, reason_if_declined)."""
    rules = db.query(TenantJobRule).filter(TenantJobRule.tenant_id == tenant.id).all()
    if not rules:
        return (True, False, None)

    category = (lead.category or "").lower()

    # Blacklist takes priority
    for rule in rules:
        if rule.rule_type == "blacklist" and rule.category_pattern.lower() in category:
            return (False, False, f"Blacklisted: {rule.category_pattern}")

    # Wantlist check
    is_wantlisted = any(
        r.rule_type == "wantlist" and r.category_pattern.lower() in category
        for r in rules
    )

    # If whitelist rules exist, category must match at least one
    # (wantlisted categories are implicitly whitelisted)
    whitelist_rules = [r for r in rules if r.rule_type == "whitelist"]
    if whitelist_rules and not is_wantlisted:
        matched = any(r.category_pattern.lower() in category for r in whitelist_rules)
        if not matched:
            return (False, False, f"Not in whitelist (category: {lead.category})")

    return (True, is_wantlisted, None)


# ── Pass 3: Offer selection ──────────────────────────────────────────────────

def _compute_offers(
    db: Session, lead: Lead, tenant: Tenant
) -> tuple[NearestBase | None, str | None, TenantSpecial | None, list[TenantSpecial]]:
    """Geocode, compute distances, find qualifying specials.

    Returns (nearest_base, pricing_tier, best_offer, other_offers).
    """
    # Geocode lead
    coords = geocode_address(
        db, lead.address_line1, lead.city, lead.state, lead.postal_code
    )

    nearest_base: NearestBase | None = None
    distance_mi: float | None = None

    if coords:
        lead.lat, lead.lng = coords
        lead.geocode_source = "api"

        # Find nearest home base
        home_bases = (
            db.query(TenantHomeBase)
            .filter(TenantHomeBase.tenant_id == tenant.id)
            .all()
        )
        for hb in home_bases:
            d = haversine_miles(coords[0], coords[1], hb.lat, hb.lng)
            if nearest_base is None or d < nearest_base.distance_mi:
                nearest_base = NearestBase(name=hb.name, distance_mi=d, lat=hb.lat, lng=hb.lng)
                distance_mi = d

    # Pricing tier from tenant config
    pricing_tier: str | None = None
    if distance_mi is not None and tenant.pricing_tiers:
        for tier in sorted(tenant.pricing_tiers, key=lambda t: t.get("max_mi", 9999)):
            if distance_mi <= tier.get("max_mi", 9999):
                pricing_tier = tier.get("text")
                break

    # Find qualifying specials
    specials = (
        db.query(TenantSpecial)
        .filter(TenantSpecial.tenant_id == tenant.id, TenantSpecial.active.is_(True))
        .all()
    )
    qualifying: list[TenantSpecial] = []
    for sp in specials:
        if _special_matches(sp, lead, distance_mi):
            qualifying.append(sp)

    best_offer = qualifying[0] if qualifying else None
    other_offers = qualifying[1:] if len(qualifying) > 1 else []

    db.flush()
    return nearest_base, pricing_tier, best_offer, other_offers


def _special_matches(special: TenantSpecial, lead: Lead, distance_mi: float | None) -> bool:
    """Evaluate a special's conditions against a lead."""
    cond = special.conditions or {}
    category = (lead.category or "").lower()

    if "category_contains" in cond:
        if cond["category_contains"].lower() not in category:
            return False

    if "max_distance_mi" in cond:
        if distance_mi is None or distance_mi > cond["max_distance_mi"]:
            return False

    if "min_distance_mi" in cond:
        if distance_mi is None or distance_mi < cond["min_distance_mi"]:
            return False

    if "urgency_in" in cond:
        if (lead.urgency or "") not in cond["urgency_in"]:
            return False

    now = dt.date.today().isoformat()
    if "valid_after" in cond and now < cond["valid_after"]:
        return False
    if "valid_before" in cond and now > cond["valid_before"]:
        return False

    return True


# ── Prompt building ──────────────────────────────────────────────────────────

def _build_system_prompt(tenant: Tenant) -> str:
    sample_block = ""
    if tenant.sample_email:
        sample_block = f"\nSAMPLE EMAIL (match this voice/tone):\n{tenant.sample_email}\n"

    extra = ""
    if tenant.llm_system_prompt:
        extra = f"\n{tenant.llm_system_prompt}\n"

    return f"""You are an outreach email writer for {tenant.name}, a home services company.

Your job: Write a brief, warm, personalized email body to a potential customer who
just submitted a service request through Angi. The email should feel human, not
templated. Match the voice and tone of the sample email below.

RULES:
- Write ONLY the email body text (no subject line, no "Hi Name," greeting, no sign-off — those are added separately)
- Keep it to 3-5 short paragraphs, under 150 words
- Mention specific details from their request to show you read it
- If an offer/discount applies, mention it naturally (don't make it the whole email)
- If this is a repeat customer, acknowledge it warmly
- If this is a "wantlist" category, convey genuine excitement about the work
- Never fabricate details about the company beyond what's provided
- Your first line MUST be either "DECISION: SEND" or "DECISION: SKIP"
  - Use SKIP only if prior_leads data shows this is clearly the same request resubmitted
    (same person, same category, same timeframe, no new information)
  - When in doubt, SEND
{sample_block}{extra}"""


def _build_user_prompt(ctx: PersonalizationContext) -> str:
    lead = ctx.lead
    parts = [
        f"LEAD DETAILS:",
        f"- Name: {lead.first_name} {lead.last_name}",
        f"- Category: {lead.category}",
        f'- Description: "{lead.description}"',
        f"- Urgency: {lead.urgency}",
        f"- Location: {lead.city}, {lead.state} {lead.postal_code}",
    ]

    if ctx.prior_leads:
        parts.append("\nPRIOR LEADS FROM THIS CUSTOMER (last 7 days):")
        for pl in ctx.prior_leads:
            desc = (pl.description or "")[:100]
            parts.append(f'- {pl.created_at:%Y-%m-%d}: {pl.category} — "{desc}"')

    if ctx.is_wantlisted:
        parts.append("\nNOTE: This is a high-priority job category for us. Show extra enthusiasm.")

    if ctx.best_offer:
        parts.append(
            f"\nAVAILABLE OFFER: {ctx.best_offer.name} — {ctx.best_offer.discount_text}"
        )
        if ctx.best_offer.description:
            parts.append(f"({ctx.best_offer.description})")

    if ctx.nearest_base:
        parts.append(
            f"\nNEAREST LOCATION: {ctx.nearest_base.name} "
            f"({ctx.nearest_base.distance_mi:.1f} miles away)"
        )
        if ctx.pricing_tier:
            parts.append(f"PRICING: {ctx.pricing_tier} (based on proximity)")

    if ctx.other_offers:
        names = ", ".join(o.name + " — " + o.discount_text for o in ctx.other_offers)
        parts.append(f"\nOTHER APPLICABLE OFFERS: {names}")

    if ctx.tenant.phone:
        parts.append(f"\nCOMPANY PHONE: {ctx.tenant.phone}")

    return "\n".join(parts)


# ── Main orchestrator ────────────────────────────────────────────────────────

def personalize_outbound(db: Session, msg: OutboundMessage) -> bool:
    """Run the 3-pass personalization pipeline.

    Mutates msg in-place (body_html, body_text, subject, metadata).
    Returns True if email should be sent, False if declined/skipped.
    """
    lead = msg.lead
    tenant = lead.tenant if lead else None
    if not lead or not tenant:
        log.warning("Cannot personalize msg %s — missing lead or tenant", msg.id)
        return True  # fall through to Jinja2

    msg.status = "generating"
    db.flush()

    # --- Pass 1: Repeat customer check ----------------------------------------
    prior_leads = _check_repeat_customer(db, lead, tenant)

    # --- Pass 2: Job desirability ---------------------------------------------
    should_send, is_wantlisted, decline_reason = _check_job_rules(db, lead, tenant)
    if not should_send:
        msg.status = "declined"
        msg.generation_method = "declined"
        db.add(LeadEvent(
            lead_id=lead.id,
            tenant_id=tenant.id,
            event_type="email_declined",
            payload={"reason": decline_reason},
        ))
        db.flush()
        log.info("Lead %s declined: %s", lead.id, decline_reason)
        return False

    # --- Pass 3: Offer selection ----------------------------------------------
    nearest_base, pricing_tier, best_offer, other_offers = _compute_offers(db, lead, tenant)

    # --- Assemble context -----------------------------------------------------
    ctx = PersonalizationContext(
        lead=lead,
        tenant=tenant,
        prior_leads=prior_leads,
        is_wantlisted=is_wantlisted,
        lead_lat=lead.lat,
        lead_lng=lead.lng,
        nearest_base=nearest_base,
        pricing_tier=pricing_tier,
        best_offer=best_offer,
        other_offers=other_offers,
    )

    system_prompt = _build_system_prompt(tenant)
    user_prompt = _build_user_prompt(ctx)

    # --- LLM call -------------------------------------------------------------
    decision, body_text, duration_ms = generate_email(system_prompt, user_prompt)

    # Store audit info
    msg.llm_model = settings.openai_model
    msg.llm_duration_ms = duration_ms
    msg.personalization_context = {
        "prior_leads_count": len(prior_leads),
        "is_wantlisted": is_wantlisted,
        "nearest_base": nearest_base.name if nearest_base else None,
        "distance_mi": round(nearest_base.distance_mi, 2) if nearest_base else None,
        "pricing_tier": pricing_tier,
        "best_offer": best_offer.name if best_offer else None,
        "other_offers": [o.name for o in other_offers],
    }

    if decision == "SKIP":
        msg.status = "skipped"
        msg.generation_method = "skipped"
        db.add(LeadEvent(
            lead_id=lead.id,
            tenant_id=tenant.id,
            event_type="email_skipped",
            payload={"reason": "LLM decided repeat customer — skip"},
        ))
        db.flush()
        log.info("Lead %s skipped by LLM (repeat customer)", lead.id)
        return False

    # --- Build final email ----------------------------------------------------
    # Subject is deterministic; body is from LLM
    offer_suffix = f" | {best_offer.discount_text}" if best_offer else ""
    msg.subject = f"{tenant.name} — ready to help with {lead.category or 'your project'}!{offer_suffix}"

    # Wrap body in greeting + sign-off
    greeting = f"Hi {lead.first_name},"
    signoff = f"\nWarm regards,\nThe {tenant.name} Team"
    if tenant.phone:
        signoff += f"\n{tenant.phone}"
    full_text = f"{greeting}\n\n{body_text}\n{signoff}"

    # HTML version
    body_html_content = body_text.replace("\n\n", "</p><p>").replace("\n", "<br>")
    brand = tenant.brand_color or "#2563eb"

    # Check for signature image
    signature = db.query(TenantFile).filter(
        TenantFile.tenant_id == tenant.id, TenantFile.purpose == "signature"
    ).first()
    sig_html = ""
    if signature:
        sig_url = f"{settings.app_url}/api/v1/files/{signature.id}"
        sig_html = f'<div style="margin-top:16px;"><img src="{sig_url}" alt="Signature" style="max-width:100%;"></div>'

    msg.body_html = f"""<div style="font-family:sans-serif;max-width:600px;margin:0 auto;">
<div style="background:{brand};color:#fff;padding:16px 24px;border-radius:8px 8px 0 0;">
<h2 style="margin:0;">{tenant.name}</h2>
</div>
<div style="padding:24px;background:#fff;border:1px solid #e5e7eb;border-top:none;">
<p>Hi {lead.first_name},</p>
<p>{body_html_content}</p>
<p>Warm regards,<br>The {tenant.name} Team</p>
{"<p><a href='tel:" + tenant.phone + "' style='color:" + brand + ";'>" + tenant.phone + "</a></p>" if tenant.phone else ""}
{sig_html}
</div>
</div>"""
    msg.body_text = full_text
    msg.status = "pending"  # back to pending for send_email to pick up
    msg.generation_method = "llm"

    db.flush()
    log.info("Personalized email for lead %s (duration=%dms)", lead.id, duration_ms)
    return True
