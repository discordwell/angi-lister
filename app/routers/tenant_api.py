"""Tenant-facing API — Bearer token auth, scoped to the authenticated tenant."""

import csv
import io
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from app.models import TenantHomeBase, TenantJobRule, TenantSpecial
from app.schemas.api import (
    HomeBaseIn, HomeBaseOut, JobRuleIn, JobRuleOut,
    LeadDetail, LeadSummary, MetricsSummary,
    SpecialIn, SpecialOut, SpecialUpdate,
    TenantConfig, TenantConfigUpdate, TenantProfile, DuplicatePair,
)
from app.services.api_auth import TenantContext, require_tenant
from app.services.metrics import (
    get_duplicate_pairs,
    get_lead_detail,
    get_metrics_summary,
    get_recent_leads,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant")


# ── Profile ──────────────────────────────────────────────────────────────────

@router.get("/me", response_model=TenantProfile)
def tenant_me(ctx: TenantContext = Depends(require_tenant)):
    t = ctx.tenant
    return TenantProfile(
        id=t.id, name=t.name, slug=t.slug, email=t.email, phone=t.phone,
        brand_color=t.brand_color, timezone=t.timezone,
        personalization_enabled=t.personalization_enabled, created_at=t.created_at,
    )


# ── Leads ────────────────────────────────────────────────────────────────────

@router.get("/leads", response_model=list[LeadSummary])
def tenant_leads(
    limit: int = Query(50, ge=1, le=500),
    ctx: TenantContext = Depends(require_tenant),
):
    rows, _ = get_recent_leads(ctx.db, limit=limit, tenant_id=ctx.tenant.id)
    return [LeadSummary(**r) for r in rows]


@router.get("/leads/{lead_id}", response_model=LeadDetail)
def tenant_lead_detail(lead_id: str, ctx: TenantContext = Depends(require_tenant)):
    data = get_lead_detail(ctx.db, lead_id, tenant_id=ctx.tenant.id)
    if not data:
        raise HTTPException(status_code=404, detail="Lead not found")
    return LeadDetail(**data)


# ── Metrics ──────────────────────────────────────────────────────────────────

@router.get("/metrics", response_model=MetricsSummary)
def tenant_metrics(ctx: TenantContext = Depends(require_tenant)):
    data = get_metrics_summary(ctx.db, tenant_id=ctx.tenant.id)
    return MetricsSummary(**data)


# ── Duplicates ───────────────────────────────────────────────────────────────

@router.get("/duplicates", response_model=list[DuplicatePair])
def tenant_duplicates(
    limit: int = Query(100, ge=1, le=1000),
    ctx: TenantContext = Depends(require_tenant),
):
    rows = get_duplicate_pairs(ctx.db, limit=limit, tenant_id=ctx.tenant.id)
    return [DuplicatePair(**r) for r in rows]


@router.get("/duplicates/export")
def tenant_duplicates_export(ctx: TenantContext = Depends(require_tenant)):
    rows = get_duplicate_pairs(ctx.db, limit=10000, tenant_id=ctx.tenant.id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "lead_id", "original_lead_id", "lead_name", "original_name",
        "lead_email", "score", "evidence_summary", "created_at",
    ])
    for r in rows:
        evidence = r.get("evidence", {})
        evidence_parts = [k for k, v in evidence.items() if v is True]
        writer.writerow([
            r["lead_id"], r["original_id"], r["lead_name"], r["original_name"],
            r["lead_email"], r["score"], "; ".join(evidence_parts), r["created_at"],
        ])
    output.seek(0)
    return StreamingResponse(
        output, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=duplicate_leads_rebate.csv"},
    )


# ── Config (read/update) ────────────────────────────────────────────────────

@router.get("/config", response_model=TenantConfig)
def tenant_config_get(ctx: TenantContext = Depends(require_tenant)):
    t = ctx.tenant
    db = ctx.db
    home_bases = db.query(TenantHomeBase).filter(TenantHomeBase.tenant_id == t.id).all()
    job_rules = db.query(TenantJobRule).filter(TenantJobRule.tenant_id == t.id).all()
    specials = db.query(TenantSpecial).filter(TenantSpecial.tenant_id == t.id).all()

    return TenantConfig(
        sample_email=t.sample_email,
        pricing_tiers=t.pricing_tiers,
        personalization_enabled=t.personalization_enabled,
        intro_template=t.intro_template,
        llm_system_prompt=t.llm_system_prompt,
        brand_color=t.brand_color,
        email_from_name=t.email_from_name,
        home_bases=[HomeBaseOut(id=h.id, name=h.name, address=h.address, lat=h.lat, lng=h.lng, created_at=h.created_at) for h in home_bases],
        job_rules=[JobRuleOut(id=r.id, category_pattern=r.category_pattern, rule_type=r.rule_type, created_at=r.created_at) for r in job_rules],
        specials=[SpecialOut(id=s.id, name=s.name, description=s.description, discount_text=s.discount_text, conditions=s.conditions, active=s.active, created_at=s.created_at) for s in specials],
    )


@router.put("/config", response_model=TenantConfig)
def tenant_config_update(
    body: TenantConfigUpdate,
    ctx: TenantContext = Depends(require_tenant),
):
    t = ctx.tenant
    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(t, field, value)
    ctx.db.flush()
    ctx.db.commit()
    return tenant_config_get(ctx)


# ── Home Bases CRUD ──────────────────────────────────────────────────────────

@router.post("/home-bases", response_model=HomeBaseOut, status_code=201)
def tenant_add_home_base(body: HomeBaseIn, ctx: TenantContext = Depends(require_tenant)):
    hb = TenantHomeBase(
        tenant_id=ctx.tenant.id, name=body.name, address=body.address,
        lat=body.lat, lng=body.lng,
    )
    ctx.db.add(hb)
    ctx.db.commit()
    return HomeBaseOut(id=hb.id, name=hb.name, address=hb.address, lat=hb.lat, lng=hb.lng, created_at=hb.created_at)


@router.delete("/home-bases/{hb_id}", status_code=204)
def tenant_delete_home_base(hb_id: str, ctx: TenantContext = Depends(require_tenant)):
    hb = ctx.db.query(TenantHomeBase).filter(
        TenantHomeBase.id == hb_id, TenantHomeBase.tenant_id == ctx.tenant.id
    ).first()
    if not hb:
        raise HTTPException(status_code=404, detail="Home base not found")
    ctx.db.delete(hb)
    ctx.db.commit()
    return Response(status_code=204)


# ── Job Rules CRUD ───────────────────────────────────────────────────────────

@router.post("/job-rules", response_model=JobRuleOut, status_code=201)
def tenant_add_job_rule(body: JobRuleIn, ctx: TenantContext = Depends(require_tenant)):
    if body.rule_type not in ("whitelist", "blacklist", "wantlist"):
        raise HTTPException(status_code=422, detail="rule_type must be whitelist, blacklist, or wantlist")
    rule = TenantJobRule(
        tenant_id=ctx.tenant.id, category_pattern=body.category_pattern,
        rule_type=body.rule_type,
    )
    ctx.db.add(rule)
    ctx.db.commit()
    return JobRuleOut(id=rule.id, category_pattern=rule.category_pattern, rule_type=rule.rule_type, created_at=rule.created_at)


@router.delete("/job-rules/{rule_id}", status_code=204)
def tenant_delete_job_rule(rule_id: str, ctx: TenantContext = Depends(require_tenant)):
    rule = ctx.db.query(TenantJobRule).filter(
        TenantJobRule.id == rule_id, TenantJobRule.tenant_id == ctx.tenant.id
    ).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Job rule not found")
    ctx.db.delete(rule)
    ctx.db.commit()
    return Response(status_code=204)


# ── Specials CRUD ────────────────────────────────────────────────────────────

@router.post("/specials", response_model=SpecialOut, status_code=201)
def tenant_add_special(body: SpecialIn, ctx: TenantContext = Depends(require_tenant)):
    sp = TenantSpecial(
        tenant_id=ctx.tenant.id, name=body.name, description=body.description,
        discount_text=body.discount_text, conditions=body.conditions, active=body.active,
    )
    ctx.db.add(sp)
    ctx.db.commit()
    return SpecialOut(id=sp.id, name=sp.name, description=sp.description, discount_text=sp.discount_text, conditions=sp.conditions, active=sp.active, created_at=sp.created_at)


@router.put("/specials/{special_id}", response_model=SpecialOut)
def tenant_update_special(special_id: str, body: SpecialUpdate, ctx: TenantContext = Depends(require_tenant)):
    sp = ctx.db.query(TenantSpecial).filter(
        TenantSpecial.id == special_id, TenantSpecial.tenant_id == ctx.tenant.id
    ).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Special not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(sp, field, value)
    ctx.db.commit()
    return SpecialOut(id=sp.id, name=sp.name, description=sp.description, discount_text=sp.discount_text, conditions=sp.conditions, active=sp.active, created_at=sp.created_at)


@router.delete("/specials/{special_id}", status_code=204)
def tenant_delete_special(special_id: str, ctx: TenantContext = Depends(require_tenant)):
    sp = ctx.db.query(TenantSpecial).filter(
        TenantSpecial.id == special_id, TenantSpecial.tenant_id == ctx.tenant.id
    ).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Special not found")
    ctx.db.delete(sp)
    ctx.db.commit()
    return Response(status_code=204)
