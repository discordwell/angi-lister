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

### Row-Level Security (Multi-Tenant Isolation)
PostgreSQL RLS enforces tenant data isolation at the database level. Every tenant-owned table has `FORCE ROW LEVEL SECURITY` and a `tenant_isolation` policy that checks `current_setting('app.current_tenant', true)`. Three access modes:
- **`__bypass__`** — webhook handler, worker, migrations, seed (full access)
- **`__all__`** — admin console and admin API (read all tenants)
- **`{tenant_uuid}`** — tenant-scoped console and tenant API (sees only own data)

The context is always applied with `SET LOCAL` (transaction-scoped) via `set_tenant()`, in one of two modes:
- **One-shot** (`set_tenant(db, tid)`): lasts until the current transaction ends. After a commit, reads fail closed (RLS hides every row), so handlers must build response data before committing.
- **Session-pinned** (`set_tenant(db, tid, session_scope=True)`): an `after_begin` listener re-issues `SET LOCAL` on every new transaction for the lifetime of that Session. Used by request-scoped sessions (`get_bypass_db`, `get_console_db`, `require_tenant`, `require_admin`) and the worker/seed sessions, which commit mid-work. Because nothing is ever set at the connection level, tenant context cannot leak across requests through the connection pool.

Auth lookups (`api_keys`, `console_sessions`) run on the request's bypass session; `require_tenant`/`require_admin` then yield a separate scoped session that is closed when the request finishes.

### Return 200 Fast
The webhook handler persists the raw receipt and acknowledges immediately. Email delivery happens asynchronously via a separate worker process. This prevents Angi's retry mechanism (3 retries at 15-min intervals) from creating duplicates.

### Webhook Receipts as First-Class Records
Every authenticated POST is captured as a `WebhookReceipt` with raw headers and body, even if the payload fails validation. This supports the monitoring requirement — when Angi changes their format without warning, we have the raw data for forensics. Bodies that aren't valid JSON objects at all (truncated JSON, arrays, plain text) are captured too: the literal text is stored under a `_raw_body` key (capped at 10 KB), the receipt is marked `parse_valid=false`, and the endpoint still ACKs with the `<success>` body so Angi doesn't retry.

### Outbox Pattern
The API never sends email inline. It inserts an `OutboundMessage` row with status=pending. The worker polls for pending messages, composes the email (rendering tenant-branded templates), sends via Resend, and records the result. This gives us:
- Crash resilience (pending messages survive restarts)
- Retry capability (failed sends are retried up to 3x; crashes during processing count as attempts too, so a message that keeps raising is marked failed instead of retried forever)
- Audit trail (every send attempt is recorded)

### LLM Email Generation — SEND/SKIP Contract
The personalization engine (`services/personalization.py`) makes a single LLM call whose first line is a decision: `DECISION: SEND` (write this lead an email) or `DECISION: SKIP` (don't — it's a duplicate/resubmission the repeat-customer pass surfaced). `llm.py::generate_email` parses that line and returns `(decision, body, duration_ms)`.

The decision is parsed **before** any length validation, and the "unusably short generation" guard applies to the **body only when the decision is SEND**. A SKIP legitimately carries no body — `DECISION: SKIP` is 14 characters and is the *correct* terse output. The guard previously ran on the whole response first (`len(raw) < 20`), so a terse SKIP raised `LLMError`; `personalize_outbound` has no guard around `generate_email`, so that raise propagated to `process_outbound_message`, whose `except` clause **falls back to the Jinja2 template and sends the email** — defeating the skip and emailing the very repeat customer the model chose to suppress. Checking the SEND body (not the raw response) is also more correct: the `DECISION: SEND\n` prefix is not part of the email, so it can't pad a tiny body over the threshold. A rejected (too-short / empty) SEND generation still raises and falls back to Jinja2 by design. `tests/test_llm.py` pins the parse + the SEND/SKIP length contract (the parser was previously untested — the personalization tests mock `generate_email` out), and `tests/test_personalization.py::TestPersonalizePipeline::test_terse_llm_skip_actually_skips` pins the end-to-end skip through the real parser.

### Output Escaping in Outbound Email
Lead fields (name, description) come straight from the Angi webhook and the personalized email body comes from the LLM — both untrusted. There are two HTML render paths and both must neutralize markup:
- **Jinja2 templates** (`templates/email/intro.html`, the default/fallback path) — autoescaping is on for `.html`, so interpolation is safe by construction. The env uses `select_autoescape(("html", "xml"))` rather than a blanket `autoescape=True`, so the sibling `intro.txt` renders *without* escaping — a plain-text email must not carry HTML entities (a `&` stays `&`, not `&amp;`). The tenant's `intro_template` snippet is rendered once per context by `email.py::_render_intro_snippet`: the HTML render escapes the webhook lead fields (autoescape on), turns blank lines into `<p>`/`<br>`, and wraps the result in `markupsafe.Markup` so the outer template emits it without double-escaping; the text render leaves fields raw. The wrapper greeting lives in the `{% else %}` (default) branch since a custom template supplies its own.
- **The hand-built personalization HTML** (`services/personalization.py`) — assembled with f-strings, so it `html.escape()`s every interpolated value explicitly (element content with `quote=False`, attribute values with the quote-escaping default). The LLM body is escaped *before* blank lines are turned into `<p>`/`<br>` so the structural tags survive.

The same stored `body_html` is previewed in the console (`lead_detail.html`) inside a `sandbox=""` `srcdoc` iframe, so a preview renders inertly (no scripts, opaque origin) even if an unescaped value ever reaches it. Alert emails (`services/monitoring.py::send_alert`) likewise escape their body, which can embed attacker-controlled JSON keys surfaced by schema-drift detection.

### Append-Only Event Log
`LeadEvent` is an append-only table that records every significant state change: receipt captured, lead created, tenant mapped, email queued/sent/failed, duplicate detected, etc. Metrics are computed from these events rather than maintaining counters, which avoids drift during reprocessing.

### Duplicate Detection
Two levels:
1. **CorrelationId idempotency** — exact retries are no-ops
2. **Fingerprint matching** — normalized email + phone + address similarity detects when the same consumer submits multiple requests. Evidence is stored for rebate claims.

Scoring within a tenant is `email_match (0.4) + phone_match (0.3) + address_match (0.3)`, flagged at `>= 0.4`. A component only counts when **both** leads carry a non-blank value for it (`present_and_equal`): a blank field is meaningless and must contribute nothing. Without that guard, two unrelated leads that both omit phone and address would match each other on `"" == ""` for 0.6 and seed false positives into the rebate-claim export.

The `normalize_email`/`normalize_phone`/`present_and_equal` helpers in `services/duplicates.py` are the **canonical "same consumer" primitives**. The personalization engine's repeat-customer check (`services/personalization.py::_check_repeat_customer`) reuses them so the two agree on identity. It previously compared raw SQL equality (`Lead.email == lead.email`), which both (a) matched unrelated consumers who shared a *blank* field (`"" == ""`) — potentially making the LLM SKIP and suppress a legitimate first email — and (b) missed true resubmissions with different email casing or phone formatting (so a consumer flagged as a duplicate still got a second email). Because phone normalization isn't portable in SQL, the check loads the tenant's leads inside the 7-day window and matches in Python.

### Conversion Tracking
Leads can be marked as `booked`, `won`, or `lost` via the API or console UI. Status transitions are recorded as `LeadEvent` entries (`outcome_booked`, `outcome_won`, `outcome_lost`) with optional notes. The conversion rate KPI is `(booked + won) / (mapped + booked + won + lost)` — the denominator includes leads still in flight (`mapped`) so the rate isn't inflated by counting only resolved leads. This single formula is the canonical one: both `metrics.py::get_metrics_summary` (the dashboard tile) and `analytics.py::get_conversion_detail` (the Analytics page) compute it the same way, so the two pages always agree for the same data. The event-based funnel (`analytics.py::get_conversion_funnel`, rendered on the *same* Analytics page) deliberately does **not** compute a conversion rate of its own: a count-based `(booked + won) / (booked + won + lost)` would omit in-flight `mapped` leads and disagree with the canonical tile right beside it. It previously did, but the value was never rendered, so the divergence sat latent until a future caller could wire it up; `tests/test_analytics.py` now pins that the funnel emits no `conversion_rate` key.

The rate is a **0–1 fraction** everywhere in the service layer; every template scales it for display (`rate * 100`) and colors it with fraction-scale thresholds (e.g. `>= 0.30`). The Analytics page previously rendered the fraction verbatim (`0.5` showed as `0.5%`) and compared it against percentage-scale thresholds (`> 30`), which left the tile permanently red — `tests/test_analytics.py` now guards both the shared formula and the scaled display.

### Delivery Rate
The "Delivery Rate" KPI is `sent / (sent + failed)` over **non-simulated** messages — only **terminal send outcomes** count. Messages still in flight (`pending`/`generating`) or intentionally not sent by the personalization engine (`declined` by job rules, `skipped` by the LLM) are not delivery failures and are excluded from the denominator; when there are no completed attempts the rate is `None` (shown as `—`, not `0%`). This denominator definition is canonical, shared by `analytics.py::get_tenant_comparison` (admin per-tenant table) and `analytics.py::get_system_health` (the `email_failure_rate_24h` companion — the complement of delivery rate, `failed / (sent + failed)`). Each page applies its own scope window — the dashboard tile (`metrics.py::get_metrics_summary`) is all-time, the admin table is the last 30 days, and `get_system_health` is a fixed 24-hour snapshot — so the three agree for the same data. Three defects predated this: the dashboard divided by *every* non-simulated message (so a queue backlog, or a tenant that declines/skips most leads, showed a spuriously low rate and a red tile even when every message actually attempted was delivered); `get_tenant_comparison` counted *simulated* sends in its delivery rate while excluding them from its sibling personalization rate (and the dashboard excluded them) — an internal split; and `get_system_health` (the lone untested computation) diverged on *both* axes — it counted simulated sends in the denominator (so demo traffic from the console "simulate" button silently diluted a real failure spike below the `0.1` "critical" threshold, masking an email outage in `overall_health`) **and** anchored its sent count on `sent_at` while its failed count used `queued_at`, mixing two window cohorts in one ratio (a slow-to-send message counted as sent, but a sibling failure queued in the same window did not). All three are fixed: `get_system_health` now filters `is_simulated == False` and anchors both sides on `queued_at` (the only non-null timestamp every message carries, and the anchor `get_tenant_comparison` already used). `tests/test_delivery_rate.py` pins the shared formula, the non-simulated population, the cross-page agreement, and the system-health failure rate as the delivery-rate complement.

### Speed-to-Lead
"Speed to lead" is the median wall-clock seconds between a lead's `lead_created` event and its first `email_sent` event — the headline proof of the "return 200 fast, send async" design. One definition computes it on all three surfaces that show it: the dashboard tile (`metrics.py::get_metrics_summary`), the Analytics funnel (`analytics.py::get_conversion_funnel`), and the admin per-tenant table (`analytics.py::get_tenant_comparison`). The same data therefore reads the same everywhere; only the **window** differs by page — the dashboard tile is all-time, the analytics surfaces are windowed (last N days) — mirroring the Delivery Rate convention. An `email_sent` timestamp that precedes `lead_created` is impossible in normal flow (the worker only sends *after* ingestion), but clock skew or manual data edits can produce one; such negative deltas are dropped on every surface so a single bogus row can't pull one page's median below its siblings'. The dashboard already applied this guard; the two analytics surfaces did not, so they diverged for the same data until `tests/test_analytics.py::TestSpeedToLeadConsistency` pinned the shared guard.

### Schema Drift Monitoring
The webhook handler detects schema drift (missing/extra fields) on every receipt. A monitoring service (`app/services/monitoring.py`) provides:
- **Real-time alerting** — after parse failures, a debounced check (30-min cooldown) fires an email alert if the error rate exceeds the threshold within the configured window
- **Daily health check** — the worker runs all monitoring checks every 24 hours and sends a summary email if issues are found
- **Schema health endpoint** — `GET /api/v1/health/schema` returns drift and error rate status

Config: `ALERT_EMAIL`, `ALERT_ERROR_THRESHOLD` (default 3), `ALERT_WINDOW_MINUTES` (default 60).

### Naive-UTC Timestamps
All `DateTime` columns are `timestamp without time zone`, and every timestamp in the app is a **naive UTC** datetime produced by `app.utils.utcnow()`. Both backends return naive values on read (PostgreSQL by definition; SQLite's dialect drops UTC offsets when parsing), so writing aware datetimes creates values that don't compare cleanly with what comes back. `tests/test_time_convention.py` enforces the convention (model defaults must be naive; `dt.datetime.now(dt.UTC)` is banned outside `app/utils.py`).

## Data Model

- **tenants** — Business identity, branding, email templates (no RLS — lookup table)
- **angi_account_mappings** — ALAccountId → tenant_id (RLS)
- **webhook_receipts** — Raw capture of every authenticated POST (RLS, nullable tenant_id)
- **leads** — Normalized lead records with correlation_id uniqueness (RLS)
- **lead_events** — Append-only audit log (RLS, nullable tenant_id)
- **outbound_messages** — Email outbox with delivery status (RLS)
- **duplicate_matches** — Pairs of suspected duplicate leads with evidence (RLS)
- **tenant_home_bases** — Office locations with lat/lng for proximity scoring (RLS)
- **tenant_job_rules** — Category whitelist/blacklist/wantlist rules (RLS)
- **tenant_specials** — Promotional offers with conditions (RLS)
- **geocode_cache** — Global postal code coordinate cache (no RLS)

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
