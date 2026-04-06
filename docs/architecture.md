# Angi -> Netic Lead Integration Architecture

## What we're building and why it exists

We're building a system that can receive a lead payload that Angi would send, store it so it's not lost and can be analyzed later, and then immediately start outreach to the consumer on behalf of the correct service provider (the "tenant"). In production, this outreach might be a call or SMS from an AI agent, but for this demo it's an email that initiates the appointment booking flow.

## High-level flow

When Angi sends a lead, it will make an HTTP POST request to an endpoint that Netic provides. The endpoint accepts the lead payload, authenticates that the request is really from Angi using the API Key (or, in our demo, from the tester), saves the lead to a database, maps it to the correct tenant (because Netic serves many businesses), and then triggers an outreach action that contacts the consumer. Finally, it returns an HTTP 200 response quickly, because webhook providers typically retry if they don't get a success response, and retries are a common source of accidental duplicates.

That "return 200 fast" detail matters because it's the difference between a robust webhook integration and a brittle one. If we take too long to process or crash mid-way, Angi will send the same lead again—sometimes multiple times. So the handler should acknowledge receipt immediately and treat the POST as "received", not "fully processed." Concretely, we:

1. Persist the raw payload + a stable dedupe key (e.g., CorrelationId) as the first side effect
2. Enforce idempotency so a retry becomes a no-op (or reuses the existing record)
3. Move slower work (tenant enrichment, email/SMS, CRM writes) into a background job / queue / async task that can be retried independently

This gives us a clean way to track what arrived, when, and how we handled it while preventing duplicate outreach and keeping the webhook contract reliable even under timeouts, crashes, or provider retry storms.

## Why FastAPI

FastAPI is ideal here because this project is fundamentally about receiving structured JSON, validating it, and executing a deterministic set of backend steps. FastAPI lets us express that as "here is the schema of the request, and here is the function that handles it," without dragging in frontend routing, React builds, or Next.js server/runtime footguns. Since the deliverable is an integration and not a product UI, a lightweight API server is the shortest path to a clean, correct demo. The central artifact we're building is a webhook receiver plus orchestration logic, and FastAPI is a very direct fit for that.

## What data we store and why each piece matters

I need to store different kinds of information, which should exist in different tables inside a single database. I'll use SQLite database as separate tables because SQLite is just a single file on disk and FastAPI app reads/writes it directly. I define the following tables:

### Tenants

Netic is multi-tenant. A lead arriving from Angi must be associated with the correct business, because that changes both the operational behavior (who gets notified, what calendar is used) and the messaging behavior (branding, tone, business hours). So we create a tenants table that holds basic identity and messaging configuration.

### Mapping from Angi to tenants

Angi needs a way to indicate which service provider the lead belongs to, which is the account identifier ALAccountId in their payload. angi_mappings table simply maps ALAccountId to tenant_id. When a lead comes in, we look up that mapping to decide which tenant owns the lead.

### Leads table

The leads table is our source of truth. It stores the normalized fields we care about (name, email, phone, category, urgency, etc.) and it also stores the raw JSON payload. Storing raw payload is important because upstream systems change formats without warning. We also store a unique correlation identifier such as CorrelationId. This is the idempotency key. It exists because webhook providers retry, and retries must not create duplicates. If the same CorrelationId is posted again, we should treat it as "already processed" and return success, not insert a second lead.

### Outreach messages

When we send an email, we want to record that we sent it, otherwise we have no way to calculate speed-to-lead or prove that you responded. An outreach_messages table solves that by recording the composed subject/body and the send status (queued, sent, failed). This also makes it easy to extend later to SMS or phone calls without redesigning the system: it becomes a general "outbound communications log."

### Lead events

We're interested in per-customer metrics storage. The simplest and most flexible way to support metrics is not to maintain counters in the tenant row because counters become wrong if we reprocess, dedupe, or backfill. Instead, we want to write an "event" every time something meaningful happens, and compute metrics from those events.

To do so, we'll use an append-only lead_events table. This lets us record timestamps for "lead received," "tenant mapped," "email sent," "email failed," etc. Once we have those events, we can calculate speed-to-lead as the time difference between "lead received" and "email sent," and we can calculate volumes and failure rates by counting events by tenant. This gives us analytics capability without implementing a heavy analytics system.
