# Claudepad - Angi-Lister Session Memory

## Session Summaries

### 2026-04-06T21:30Z
Replaced fictional seed tenants (Apex HVAC, BlueWave Plumbing, Spark Electric) with real Netic customers (Hoffmann Brothers, Paschal Air, Heartland Home Services). Updated seed script, test fixtures, simulate script, console template, and README. All 26 tests pass. Deployed to production with `--reset` reseed. The mapping mechanism (ALAccountId -> tenant via angi_account_mappings) was already fully built; this change only updated the dummy data to use realistic companies.

## Key Findings

- **Netic customers (public):** Hoffmann Brothers (St. Louis HVAC), Paschal Air Plumbing & Electric (NW Arkansas), Heartland Home Services (Midwest HVAC)
- **Production DB requires `--reset` flag** when seed data structure changes, since the seed script skips if tenants already exist
- **Mapping mechanism:** ALAccountId field in Angi payload -> angi_account_mappings table -> tenant_id. Unmapped leads are stored but don't trigger outbound emails.
