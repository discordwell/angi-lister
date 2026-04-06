# Claudepad - Angi-Lister Session Memory

## Session Summaries

### 2026-04-06T22:10Z
Implemented PostgreSQL Row-Level Security (RLS) for multi-tenant architecture. Added `tenant_id` to WebhookReceipt, LeadEvent, DuplicateMatch. Created migration 004 with FORCE ROW LEVEL SECURITY + tenant_isolation policies on 11 tables. Three access modes: `__bypass__` (webhook/worker/system), `__all__` (admin), `{tenant_uuid}` (tenant user). Console routes now use tenant-scoped sessions from ConsoleSession.tenant_id. Refactored all routers: webhook/api/auth/health use get_bypass_db, console uses get_console_db. Worker and seed script set bypass. 53 SQLite tests pass. Deployed to prod, applied RLS policies, verified webhook + health working. Paschal Air is demo tenant.

### 2026-04-06T21:30Z
Replaced fictional seed tenants (Apex HVAC, BlueWave Plumbing, Spark Electric) with real Netic customers (Hoffmann Brothers, Paschal Air, Heartland Home Services). Updated seed script, test fixtures, simulate script, console template, and README. All 26 tests pass. Deployed to production with `--reset` reseed.

## Key Findings

- **Netic customers (public):** Hoffmann Brothers (St. Louis HVAC), Paschal Air Plumbing & Electric (NW Arkansas), Heartland Home Services (Midwest HVAC)
- **Production DB requires `--reset` flag** when seed data structure changes — but `--reset` drops RLS policies. Must re-run `alembic upgrade head` or re-apply policies manually after reset.
- **Mapping mechanism:** ALAccountId field in Angi payload -> angi_account_mappings table -> tenant_id. Unmapped leads are stored but don't trigger outbound emails.
- **RLS bypass on SQLite:** `_set_tenant` no-ops on non-PostgreSQL dialects (checked via `db.get_bind().dialect.name`). Existing SQLite tests work unchanged.
- **Test fixtures must override all DB deps:** `get_db`, `get_bypass_db`, `get_admin_db`, `get_console_db` all need overrides in conftest for SQLite tests.
- **Admin login:** cordwell@gmail.com / admin — console session with `tenant_id=None` gets `__all__` mode.
- **Demo login:** Paschal Air via `/auth/demo-login` — console session with specific `tenant_id` gets RLS-scoped views.
