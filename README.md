# Angi-Lister

Production webhook integration for receiving and processing consumer leads from Angi. Built for the Netic deployment engineer assessment.

**Live:** https://angi.discordwell.com

## What It Does

1. **Receives leads** via `POST /webhooks/angi/leads` with `X-API-KEY` authentication
2. **Stores everything** — raw webhook receipts, normalized leads, event audit trail
3. **Maps to tenants** — routes leads to the correct service provider via ALAccountId
4. **Sends intro emails** — branded, tenant-specific emails via Resend to start appointment booking
5. **Detects duplicates** — fingerprint matching with evidence storage for rebate claims
6. **Monitors schema drift** — flags when Angi changes their payload format

## Quick Start

```bash
# Clone and start locally
cp .env.example .env
docker compose up -d

# Seed demo tenants
docker compose exec api python -m scripts.seed

# Send a test lead
python -m scripts.simulate --count 3

# Run all test scenarios
python -m scripts.simulate --all
```

## Console

Operations dashboard at `/console` (HTTP Basic Auth):
- Real-time KPI metrics (speed-to-lead, delivery rate, duplicate rate)
- Lead detail with event timeline and email preview
- Duplicate evidence with one-click CSV export for rebate claims
- Simulate panel for live demos

## Demo Tenants

| Tenant | ALAccountId | Category |
|--------|-------------|----------|
| Apex HVAC Indianapolis | 100001 | HVAC |
| BlueWave Plumbing Co | 100002 | Plumbing |
| Spark Electric Services | 100003 | Electrical |

## API

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/webhooks/angi/leads` | POST | X-API-KEY | Angi lead ingestion |
| `/healthz` | GET | None | Liveness check |
| `/readyz` | GET | None | Readiness check |
| `/console` | GET | Basic | Operations dashboard |
| `/api/v1/metrics` | GET | Basic | KPI summary |
| `/api/v1/leads` | GET | Basic | Lead list |
| `/api/v1/leads/{id}` | GET | Basic | Lead detail |
| `/api/v1/duplicates` | GET | Basic | Duplicate pairs |

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
