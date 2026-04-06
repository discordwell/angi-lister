# Architecture

## System Overview

Angi-Lister is a production webhook integration that receives consumer leads from Angi, maps them to the correct service provider (tenant), and automatically initiates outreach via email.

```
Angi (HTTP POST)  ─→  Caddy (TLS)  ─→  FastAPI API  ─→  PostgreSQL
                                              │
                                              ├── WebhookReceipt (raw capture)
                                              ├── Lead (normalized)
                                              ├── LeadEvent (audit trail)
                                              └── OutboundMessage (outbox)
                                                       │
                                              Worker ───┘── Resend API ─→ Consumer Email
```

## Key Design Decisions

### Return 200 Fast
The webhook handler persists the raw receipt and acknowledges immediately. Email delivery happens asynchronously via a separate worker process. This prevents Angi's retry mechanism (3 retries at 15-min intervals) from creating duplicates.

### Webhook Receipts as First-Class Records
Every authenticated POST is captured as a `WebhookReceipt` with raw headers and body, even if the payload fails validation. This supports the monitoring requirement — when Angi changes their format without warning, we have the raw data for forensics.

### Outbox Pattern
The API never sends email inline. It inserts an `OutboundMessage` row with status=pending. The worker polls for pending messages, composes the email (rendering tenant-branded templates), sends via Resend, and records the result. This gives us:
- Crash resilience (pending messages survive restarts)
- Retry capability (failed sends are retried up to 3x)
- Audit trail (every send attempt is recorded)

### Append-Only Event Log
`LeadEvent` is an append-only table that records every significant state change: receipt captured, lead created, tenant mapped, email queued/sent/failed, duplicate detected, etc. Metrics are computed from these events rather than maintaining counters, which avoids drift during reprocessing.

### Duplicate Detection
Two levels:
1. **CorrelationId idempotency** — exact retries are no-ops
2. **Fingerprint matching** — normalized email + phone + address similarity detects when the same consumer submits multiple requests. Evidence is stored for rebate claims.

## Data Model

- **tenants** — Business identity, branding, email templates
- **angi_account_mappings** — ALAccountId → tenant_id
- **webhook_receipts** — Raw capture of every authenticated POST
- **leads** — Normalized lead records with correlation_id uniqueness
- **lead_events** — Append-only audit log
- **outbound_messages** — Email outbox with delivery status
- **duplicate_matches** — Pairs of suspected duplicate leads with evidence

## Stack

- Python 3.12 + FastAPI
- PostgreSQL 16
- SQLAlchemy 2.0 (sync)
- Alembic (migrations)
- Jinja2 + HTMX + Tailwind CSS (console UI)
- Resend (email delivery via REST API)
- Docker Compose (db + api + worker)
- Caddy (TLS + reverse proxy)

## Deployment

Hosted at https://angi.discordwell.com on OVH VPS. Caddy handles TLS auto-provisioning and reverse proxies to the Docker Compose stack on port 8090.

```
OVH-2 (15.204.59.61)
├── Caddy (:443) → reverse proxy → localhost:8090
└── Docker Compose
    ├── db (postgres:16-alpine)
    ├── api (FastAPI, :8090→:8000)
    └── worker (email delivery loop)
```
