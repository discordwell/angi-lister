"""Admin API — for Netic platform operators to manage tenants, keys, and mappings."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.models import AngiMapping, ApiKey, Tenant
from app.schemas.api import (
    ApiKeyCreate, ApiKeyCreated, ApiKeyOut,
    LeadSummary, MappingIn, MappingOut, MetricsSummary,
    TenantCreate, TenantProfile, TenantUpdate,
)
from app.services.api_auth import AdminContext, require_admin, generate_api_key
from app.services.metrics import get_metrics_summary, get_recent_leads

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin")


# ── Tenants ──────────────────────────────────────────────────────────────────

def _tenant_to_profile(t: Tenant) -> TenantProfile:
    return TenantProfile(
        id=t.id, name=t.name, slug=t.slug, email=t.email, phone=t.phone,
        brand_color=t.brand_color, timezone=t.timezone,
        personalization_enabled=t.personalization_enabled, created_at=t.created_at,
    )


@router.get("/tenants", response_model=list[TenantProfile])
def admin_list_tenants(ctx: AdminContext = Depends(require_admin)):
    tenants = ctx.db.query(Tenant).order_by(Tenant.created_at.desc()).all()
    return [_tenant_to_profile(t) for t in tenants]


@router.post("/tenants", response_model=TenantProfile, status_code=201)
def admin_create_tenant(body: TenantCreate, ctx: AdminContext = Depends(require_admin)):
    existing = ctx.db.query(Tenant).filter(Tenant.slug == body.slug).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Tenant with slug '{body.slug}' already exists")
    tenant = Tenant(**body.model_dump())
    ctx.db.add(tenant)
    ctx.db.commit()
    log.info("Admin %s created tenant %s", ctx.email, tenant.id)
    return _tenant_to_profile(tenant)


@router.put("/tenants/{tenant_id}", response_model=TenantProfile)
def admin_update_tenant(tenant_id: str, body: TenantUpdate, ctx: AdminContext = Depends(require_admin)):
    tenant = ctx.db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(tenant, field, value)
    ctx.db.commit()
    return _tenant_to_profile(tenant)


# ── API Keys ─────────────────────────────────────────────────────────────────

@router.get("/tenants/{tenant_id}/api-keys", response_model=list[ApiKeyOut])
def admin_list_api_keys(tenant_id: str, ctx: AdminContext = Depends(require_admin)):
    keys = (
        ctx.db.query(ApiKey)
        .filter(ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc())
        .all()
    )
    return [
        ApiKeyOut(
            id=k.id, name=k.name, key_prefix=k.key_prefix,
            is_admin=k.is_admin, last_used_at=k.last_used_at, created_at=k.created_at,
        )
        for k in keys
    ]


@router.post("/tenants/{tenant_id}/api-keys", response_model=ApiKeyCreated, status_code=201)
def admin_create_api_key(
    tenant_id: str, body: ApiKeyCreate, ctx: AdminContext = Depends(require_admin),
):
    tenant = ctx.db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    record, raw_key = generate_api_key(
        ctx.db, tenant_id=tenant_id, name=body.name, is_admin=body.is_admin,
    )
    ctx.db.commit()
    log.info("Admin %s created API key %s for tenant %s", ctx.email, record.id, tenant_id)
    return ApiKeyCreated(
        id=record.id, name=record.name, key_prefix=record.key_prefix,
        is_admin=record.is_admin, last_used_at=record.last_used_at,
        created_at=record.created_at, raw_key=raw_key,
    )


@router.delete("/tenants/{tenant_id}/api-keys/{key_id}", status_code=204)
def admin_revoke_api_key(
    tenant_id: str, key_id: str, ctx: AdminContext = Depends(require_admin),
):
    import datetime as dt
    key = ctx.db.query(ApiKey).filter(
        ApiKey.id == key_id, ApiKey.tenant_id == tenant_id,
    ).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    key.revoked_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    ctx.db.commit()
    log.info("Admin %s revoked API key %s", ctx.email, key_id)
    return Response(status_code=204)


# ── Mappings ─────────────────────────────────────────────────────────────────

@router.post("/tenants/{tenant_id}/mappings", response_model=MappingOut, status_code=201)
def admin_add_mapping(
    tenant_id: str, body: MappingIn, ctx: AdminContext = Depends(require_admin),
):
    tenant = ctx.db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    existing = ctx.db.query(AngiMapping).filter(
        AngiMapping.al_account_id == body.al_account_id
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"ALAccountId '{body.al_account_id}' is already mapped to tenant {existing.tenant_id}",
        )
    mapping = AngiMapping(al_account_id=body.al_account_id, tenant_id=tenant_id)
    ctx.db.add(mapping)
    ctx.db.commit()
    return MappingOut(id=mapping.id, al_account_id=mapping.al_account_id, tenant_id=mapping.tenant_id)


# ── Global metrics and leads ─────────────────────────────────────────────────

@router.get("/metrics", response_model=MetricsSummary)
def admin_metrics(ctx: AdminContext = Depends(require_admin)):
    data = get_metrics_summary(ctx.db)
    return MetricsSummary(**data)


@router.get("/leads", response_model=list[LeadSummary])
def admin_leads(
    limit: int = Query(50, ge=1, le=500),
    ctx: AdminContext = Depends(require_admin),
):
    rows = get_recent_leads(ctx.db, limit=limit)
    return [LeadSummary(**r) for r in rows]
