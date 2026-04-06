# Angi-Lister

Production webhook integration for receiving and processing consumer leads from Angi. Built for the Netic deployment engineer assessment.

**Live:** https://angi.discordwell.com
**ngrok:** https://uneffected-unlevelly-ricky.ngrok-free.dev

## Try It Now

Send a lead — no API key needed:

```bash
curl -X POST https://uneffected-unlevelly-ricky.ngrok-free.dev/webhooks/demo/leads \
  -H "Content-Type: application/json" \
  -d '{
    "FirstName": "Bob",
    "LastName": "Builder",
    "PhoneNumber": "5554332646",
    "Email": "bob.builder@gmail.com",
    "Source": "Angie'\''s List Quote Request",
    "Description": "I'\''m looking for recurring house cleaning services please.",
    "Category": "Indianapolis – House Cleaning",
    "Urgency": "This Week"
  }'
```

The demo endpoint auto-assigns leads to **Paschal Air, Plumbing & Electric** and auto-generates a CorrelationId. Then check the console to see it arrive.

## Endpoints

### For evaluators (no auth required)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/webhooks/demo/leads` | POST | Send test leads — no API key, defaults to demo tenant |
| `/healthz` | GET | Liveness check |
| `/readyz` | GET | Readiness check (DB + worker) |

### Production webhook (what Angi would use)

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/webhooks/angi/leads` | POST | `X-API-KEY` header | Angi lead ingestion (full auth) |

### Console & API

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/console` | GET | Session (magic link) | Operations dashboard, lead management |
| `/console/analytics` | GET | Session | Lead volume, conversion funnel, duplicate rebates |
| `/console/analytics/admin` | GET | Session (admin only) | Cross-tenant comparison, system health |
| `/api/v1/metrics` | GET | Bearer / Session | KPI summary JSON |
| `/api/v1/leads` | GET | Bearer / Session | Lead list JSON |
| `/api/v1/leads/{id}` | GET | Bearer / Session | Lead detail JSON |
| `/api/v1/duplicates/export` | GET | Bearer / Session | CSV export for rebate claims |

## What It Does

1. **Receives leads** via webhook — Angi JSON payload with X-API-KEY auth (or demo route without)
2. **Stores everything** — raw webhook receipts, normalized leads, event audit trail
3. **Maps to tenants** — routes leads to the correct service provider via ALAccountId
4. **Sends intro emails** — branded, AI-personalized emails via Resend (<1s speed-to-lead)
5. **Detects duplicates** — fingerprint matching with evidence storage and CSV export for rebate claims
6. **Monitors schema drift** — flags when Angi changes their payload format
7. **Tracks conversion** — Mark leads as Live/Dead, analytics show conversion rate and funnel

## Demo Tenants

| Tenant | ALAccountId | Category |
|--------|-------------|----------|
| Hoffmann Brothers | 100001 | HVAC / Plumbing / Electrical |
| **Paschal Air, Plumbing & Electric** | **100002** | **HVAC / Plumbing / Electrical** (demo default) |
| Heartland Home Services | 100003 | HVAC |

## Console Access

Visit the console to see leads arrive in real-time:
- **URL:** https://angi.discordwell.com/console
- Click **"Demo as Paschal Air"** to log in instantly
- Click **"Demo as Admin"** to see cross-tenant analytics

## Quick Start (Local Dev)

```bash
cp .env.example .env
docker compose up -d
docker compose exec api alembic upgrade head
docker compose exec api python -m scripts.seed
python -m scripts.simulate --count 5
```

## Testing

```bash
pytest tests/ -v
```

## Deploy

```bash
./infra/deploy.sh
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed system design.
