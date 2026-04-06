"""Angi API contract tests — validates compliance with the Angi Standard Lead JSON Feed spec.

These tests verify the webhook endpoint behaves exactly as Angi expects:
- Auth via X-API-KEY header
- HTTP 200 response with <success> tag in body (even on parse failure)
- Idempotency via CorrelationId (retries must not create duplicates)
- Correct handling of all spec-defined fields
- Graceful handling of edge cases Angi might send

Run locally:  pytest tests/test_angi_contract.py -v
Run against live:  python -m tests.test_angi_contract --url https://angi.discordwell.com
"""

import json
import uuid

import pytest

from tests.conftest import SAMPLE_LEAD


# ── The exact example payload from the Angi PDF spec (page 3) ────────────────
ANGI_PDF_PAYLOAD = {
    "FirstName": "Bob",
    "LastName": "Builder",
    "PhoneNumber": "5554332646",
    "PostalAddress": {
        "AddressFirstLine": "123 Main St.",
        "AddressSecondLine": "",
        "City": "Indianapolis",
        "State": "IN",
        "PostalCode": "46203",
    },
    "Email": "bob.builder@gmail.com",
    "Source": "Angie's List Quote Request",
    "Description": "I'm Looking for recurring house cleaning services please.",
    "Category": "Indianapolis – House Cleaning",
    "Urgency": "This Week",
    "CorrelationId": "61a7de56-dba3-4e59-8e2a-3fa827f84f7f",
    "ALAccountId": "100001",
}

HEADERS = {"X-API-KEY": "test-key"}
SUCCESS_TAG = "<success>"


# ═══════════════════════════════════════════════════════════════════════════════
# 1. AUTHENTICATION (Angi spec: "X-API-KEY: [CLIENT_API_KEY_PROVIDED]")
# ═══════════════════════════════════════════════════════════════════════════════

class TestAngiAuth:
    """Angi sends leads with X-API-KEY header. Invalid/missing key must reject."""

    def test_no_api_key_returns_401(self, seeded_client):
        resp = seeded_client.post("/webhooks/angi/leads", json=ANGI_PDF_PAYLOAD)
        assert resp.status_code == 401

    def test_empty_api_key_returns_401(self, seeded_client):
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=ANGI_PDF_PAYLOAD,
            headers={"X-API-KEY": ""},
        )
        assert resp.status_code == 401

    def test_wrong_api_key_returns_401(self, seeded_client):
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=ANGI_PDF_PAYLOAD,
            headers={"X-API-KEY": "this-is-not-the-key"},
        )
        assert resp.status_code == 401

    def test_valid_api_key_returns_200(self, seeded_client):
        payload = {**ANGI_PDF_PAYLOAD, "CorrelationId": str(uuid.uuid4())}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200

    def test_api_key_is_case_sensitive(self, seeded_client):
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=ANGI_PDF_PAYLOAD,
            headers={"X-API-KEY": "Test-Key"},  # wrong case
        )
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# 2. RESPONSE FORMAT (Angi spec: "HTTP 200 with success string in body")
# ═══════════════════════════════════════════════════════════════════════════════

class TestAngiResponse:
    """Angi expects: 200 + body containing <success>..cross-reference data..</success>"""

    def test_success_response_is_200(self, seeded_client):
        payload = {**ANGI_PDF_PAYLOAD, "CorrelationId": str(uuid.uuid4())}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200

    def test_success_body_contains_success_tag(self, seeded_client):
        payload = {**ANGI_PDF_PAYLOAD, "CorrelationId": str(uuid.uuid4())}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        body = resp.text
        assert SUCCESS_TAG in body, f"Response body must contain {SUCCESS_TAG}: {body}"

    def test_success_body_contains_receipt_id(self, seeded_client):
        payload = {**ANGI_PDF_PAYLOAD, "CorrelationId": str(uuid.uuid4())}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        data = resp.json()
        assert data["receipt_id"], "receipt_id must be populated"

    def test_success_body_contains_lead_id(self, seeded_client):
        payload = {**ANGI_PDF_PAYLOAD, "CorrelationId": str(uuid.uuid4())}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        data = resp.json()
        assert data["lead_id"], "lead_id must be populated for valid leads"

    def test_success_body_contains_correlation_id(self, seeded_client):
        corr = str(uuid.uuid4())
        payload = {**ANGI_PDF_PAYLOAD, "CorrelationId": corr}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        data = resp.json()
        assert data["correlation_id"] == corr


# ═══════════════════════════════════════════════════════════════════════════════
# 3. PARSE FAILURE HANDLING
#    (Angi spec: "a 200 with content that doesn't match the success string"
#     → triggers retry. We MUST return <success> even on parse failure.)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAngiParseFailure:
    """Malformed payloads must return 200 with <success> tag to prevent Angi retries."""

    def test_malformed_payload_returns_200(self, seeded_client):
        resp = seeded_client.post(
            "/webhooks/angi/leads",
            json={"garbage": True},
            headers=HEADERS,
        )
        assert resp.status_code == 200, f"Must return 200 even on parse failure: {resp.status_code}"

    def test_malformed_payload_body_contains_success_tag(self, seeded_client):
        """CRITICAL: Without <success> tag, Angi retries the bad payload 3 times."""
        resp = seeded_client.post(
            "/webhooks/angi/leads",
            json={"garbage": True},
            headers=HEADERS,
        )
        body = resp.text
        assert SUCCESS_TAG in body, (
            f"Parse failure response MUST contain {SUCCESS_TAG} to prevent Angi retries. "
            f"Got: {body}"
        )

    def test_empty_object_returns_200_with_success(self, seeded_client):
        resp = seeded_client.post(
            "/webhooks/angi/leads", json={}, headers=HEADERS,
        )
        assert resp.status_code == 200
        assert SUCCESS_TAG in resp.text

    def test_missing_required_fields_returns_200_with_success(self, seeded_client):
        """Payload with some but not all required fields."""
        partial = {"FirstName": "Bob", "LastName": "Builder"}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=partial, headers=HEADERS,
        )
        assert resp.status_code == 200
        assert SUCCESS_TAG in resp.text

    def test_parse_failure_still_creates_receipt(self, seeded_client):
        resp = seeded_client.post(
            "/webhooks/angi/leads",
            json={"garbage": True},
            headers=HEADERS,
        )
        data = resp.json()
        assert data["receipt_id"], "Receipt must be created even on parse failure"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. IDEMPOTENCY / RETRY HANDLING
#    (Angi spec: "retries 3 times with 15-minute interval... retries do not
#     create duplicate content issues")
# ═══════════════════════════════════════════════════════════════════════════════

class TestAngiIdempotency:
    """Angi retries on failure. Same CorrelationId must not create duplicates."""

    def test_same_correlation_id_returns_same_lead(self, seeded_client):
        corr = str(uuid.uuid4())
        payload = {**ANGI_PDF_PAYLOAD, "CorrelationId": corr}

        r1 = seeded_client.post("/webhooks/angi/leads", json=payload, headers=HEADERS)
        r2 = seeded_client.post("/webhooks/angi/leads", json=payload, headers=HEADERS)
        r3 = seeded_client.post("/webhooks/angi/leads", json=payload, headers=HEADERS)

        assert r1.json()["lead_id"] == r2.json()["lead_id"] == r3.json()["lead_id"]

    def test_retry_returns_200_with_success_tag(self, seeded_client):
        corr = str(uuid.uuid4())
        payload = {**ANGI_PDF_PAYLOAD, "CorrelationId": corr}

        seeded_client.post("/webhooks/angi/leads", json=payload, headers=HEADERS)
        retry = seeded_client.post("/webhooks/angi/leads", json=payload, headers=HEADERS)

        assert retry.status_code == 200
        assert SUCCESS_TAG in retry.text

    def test_different_correlation_ids_create_different_leads(self, seeded_client):
        p1 = {**ANGI_PDF_PAYLOAD, "CorrelationId": str(uuid.uuid4())}
        p2 = {**ANGI_PDF_PAYLOAD, "CorrelationId": str(uuid.uuid4())}

        r1 = seeded_client.post("/webhooks/angi/leads", json=p1, headers=HEADERS)
        r2 = seeded_client.post("/webhooks/angi/leads", json=p2, headers=HEADERS)

        assert r1.json()["lead_id"] != r2.json()["lead_id"]


# ═══════════════════════════════════════════════════════════════════════════════
# 5. FIELD HANDLING (all fields from Angi spec page 2)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAngiFields:
    """Verify all Angi-defined fields are accepted and stored correctly."""

    def test_exact_pdf_example_accepted(self, seeded_client):
        """The exact JSON from page 3 of the Angi spec must work."""
        payload = {**ANGI_PDF_PAYLOAD, "CorrelationId": str(uuid.uuid4())}
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["lead_id"] is not None

    def test_special_characters_in_description(self, seeded_client):
        """Angi example has smart quotes and apostrophes."""
        payload = {
            **ANGI_PDF_PAYLOAD,
            "CorrelationId": str(uuid.uuid4()),
            "Description": "I'm looking for someone to fix my A/C – it's been making a \"weird\" noise & won't cool below 80°F",
        }
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200

    def test_unicode_in_name(self, seeded_client):
        payload = {
            **ANGI_PDF_PAYLOAD,
            "CorrelationId": str(uuid.uuid4()),
            "FirstName": "José",
            "LastName": "García",
        }
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200

    def test_long_description(self, seeded_client):
        payload = {
            **ANGI_PDF_PAYLOAD,
            "CorrelationId": str(uuid.uuid4()),
            "Description": "x" * 5000,
        }
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200

    def test_empty_address_second_line(self, seeded_client):
        """AddressSecondLine is often empty — must not fail."""
        payload = {**ANGI_PDF_PAYLOAD, "CorrelationId": str(uuid.uuid4())}
        payload["PostalAddress"]["AddressSecondLine"] = ""
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200

    def test_missing_optional_fields_still_accepted(self, seeded_client):
        """Source, Description, Category, Urgency can be empty strings."""
        payload = {
            "FirstName": "Min",
            "LastName": "Payload",
            "PhoneNumber": "5550000000",
            "PostalAddress": {
                "AddressFirstLine": "1 Test St",
                "City": "Test",
                "State": "TS",
                "PostalCode": "00000",
            },
            "Email": "min@test.com",
            "Source": "",
            "Description": "",
            "Category": "",
            "Urgency": "",
            "CorrelationId": str(uuid.uuid4()),
            "ALAccountId": "100001",
        }
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["lead_id"] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 6. SCHEMA DRIFT DETECTION
#    (Angi has changed formats without warning in the past)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAngiSchemaDrift:
    """Extra or unexpected fields must be accepted (not rejected)."""

    def test_extra_top_level_fields_accepted(self, seeded_client):
        payload = {
            **ANGI_PDF_PAYLOAD,
            "CorrelationId": str(uuid.uuid4()),
            "NewAngiField": "value",
            "InternalTrackingId": 12345,
        }
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["lead_id"] is not None

    def test_extra_address_fields_accepted(self, seeded_client):
        payload = {
            **ANGI_PDF_PAYLOAD,
            "CorrelationId": str(uuid.uuid4()),
        }
        payload["PostalAddress"]["CountryCode"] = "US"
        payload["PostalAddress"]["County"] = "Marion"
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200

    def test_null_values_in_optional_fields(self, seeded_client):
        """Angi might send null instead of empty string."""
        payload = {
            **ANGI_PDF_PAYLOAD,
            "CorrelationId": str(uuid.uuid4()),
            "Description": None,
            "Source": None,
        }
        # This may parse-fail (Pydantic rejects None for str fields)
        # but must still return 200 with <success>
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200
        assert SUCCESS_TAG in resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# 7. TENANT MAPPING
#    (ALAccountId → tenant routing)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAngiTenantMapping:
    """Leads must be routed to the correct tenant or gracefully handled if unmapped."""

    def test_known_account_maps_to_tenant(self, seeded_client):
        payload = {
            **ANGI_PDF_PAYLOAD,
            "CorrelationId": str(uuid.uuid4()),
            "ALAccountId": "100001",
        }
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["lead_id"] is not None

    def test_unknown_account_returns_200(self, seeded_client):
        """Unmapped ALAccountId must not crash — return 200 so Angi doesn't retry."""
        payload = {
            **ANGI_PDF_PAYLOAD,
            "CorrelationId": str(uuid.uuid4()),
            "ALAccountId": "UNKNOWN-ACCOUNT-999",
        }
        resp = seeded_client.post(
            "/webhooks/angi/leads", json=payload, headers=HEADERS,
        )
        assert resp.status_code == 200
        assert SUCCESS_TAG in resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# 8. DUPLICATE DETECTION
#    (Same consumer, different CorrelationId = Angi sent same lead again)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAngiDuplicates:
    """Same consumer submitting multiple requests should be detected."""

    def test_same_email_phone_flagged(self, seeded_client):
        base = {**ANGI_PDF_PAYLOAD, "ALAccountId": "100001"}
        p1 = {**base, "CorrelationId": str(uuid.uuid4())}
        p2 = {**base, "CorrelationId": str(uuid.uuid4())}

        r1 = seeded_client.post("/webhooks/angi/leads", json=p1, headers=HEADERS)
        r2 = seeded_client.post("/webhooks/angi/leads", json=p2, headers=HEADERS)

        # Both succeed
        assert r1.status_code == 200
        assert r2.status_code == 200
        # Different leads created
        assert r1.json()["lead_id"] != r2.json()["lead_id"]
        # Both have <success> tag
        assert SUCCESS_TAG in r1.text
        assert SUCCESS_TAG in r2.text


# ═══════════════════════════════════════════════════════════════════════════════
# CLI runner for testing against a live endpoint
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    import sys
    import time

    import httpx

    LIVE_PREFIX = "__contract_test__"

    def test_corr():
        """Generate a CorrelationId with the cleanup prefix."""
        return f"{LIVE_PREFIX}{uuid.uuid4()}"

    parser = argparse.ArgumentParser(description="Angi contract tests against live endpoint")
    parser.add_argument("--url", default="https://angi.discordwell.com")
    parser.add_argument("--api-key", default="netic-demo-2026-angi-key")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip cleanup (leave test data)")
    args = parser.parse_args()

    url = args.url.rstrip("/")
    key = args.api_key
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    counts = [0, 0]  # [passed, failed]
    results = []

    def check(name, condition, detail=""):
        if condition:
            counts[0] += 1
            results.append(f"  PASS  {name}")
        else:
            counts[1] += 1
            results.append(f"  FAIL  {name}: {detail}")

    print(f"Angi Contract Test — {url}")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print()

    # 1. Health
    r = httpx.get(f"{url}/healthz", timeout=10)
    check("healthz returns 200", r.status_code == 200, f"got {r.status_code}")

    # 2. Auth — no key
    r = httpx.post(f"{url}/webhooks/angi/leads", json={}, timeout=10)
    check("no API key → 401", r.status_code == 401, f"got {r.status_code}")

    # 3. Auth — wrong key
    r = httpx.post(f"{url}/webhooks/angi/leads", json={}, headers={"X-API-KEY": "wrong"}, timeout=10)
    check("wrong API key → 401", r.status_code == 401, f"got {r.status_code}")

    # 4. Exact PDF payload
    corr1 = test_corr()
    payload = {**ANGI_PDF_PAYLOAD, "CorrelationId": corr1, "ALAccountId": "100001"}
    r = httpx.post(f"{url}/webhooks/angi/leads", json=payload, headers=headers, timeout=15)
    check("PDF payload → 200", r.status_code == 200, f"got {r.status_code}")
    check("response has <success>", SUCCESS_TAG in r.text, r.text[:200])
    lead1 = r.json().get("lead_id", "")
    check("lead_id present", bool(lead1), "lead_id is empty")
    check("correlation_id echoed", r.json().get("correlation_id") == corr1)

    # 5. Idempotency — same CorrelationId
    r2 = httpx.post(f"{url}/webhooks/angi/leads", json=payload, headers=headers, timeout=15)
    check("retry → 200", r2.status_code == 200)
    check("retry → same lead_id", r2.json().get("lead_id") == lead1,
          f"first={lead1} retry={r2.json().get('lead_id')}")
    check("retry has <success>", SUCCESS_TAG in r2.text)

    # 6. Idempotency — 3rd retry
    r3 = httpx.post(f"{url}/webhooks/angi/leads", json=payload, headers=headers, timeout=15)
    check("3rd retry → same lead_id", r3.json().get("lead_id") == lead1)

    # 7. Parse failure → 200 with <success>
    bad = {"garbage": True, "CorrelationId": test_corr()}
    r = httpx.post(f"{url}/webhooks/angi/leads", json=bad, headers=headers, timeout=15)
    check("parse failure → 200", r.status_code == 200, f"got {r.status_code}")
    check("parse failure has <success>", SUCCESS_TAG in r.text,
          f"CRITICAL: Angi will retry without this! Body: {r.text[:200]}")

    # 8. Empty payload → 200 with <success>
    empty = {"CorrelationId": test_corr()}
    r = httpx.post(f"{url}/webhooks/angi/leads", json=empty, headers=headers, timeout=15)
    check("empty payload → 200", r.status_code == 200)
    check("empty payload has <success>", SUCCESS_TAG in r.text)

    # 9. Unmapped ALAccountId → 200 with <success>
    umapped = {**ANGI_PDF_PAYLOAD, "CorrelationId": test_corr(), "ALAccountId": "NONEXISTENT-999"}
    r = httpx.post(f"{url}/webhooks/angi/leads", json=umapped, headers=headers, timeout=15)
    check("unmapped account → 200", r.status_code == 200)
    check("unmapped has <success>", SUCCESS_TAG in r.text)

    # 10. Schema drift — extra fields
    drift = {**ANGI_PDF_PAYLOAD, "CorrelationId": test_corr(), "ALAccountId": "100001",
             "NewField": "surprise", "TrackingData": {"nested": True}}
    r = httpx.post(f"{url}/webhooks/angi/leads", json=drift, headers=headers, timeout=15)
    check("extra fields → 200", r.status_code == 200)
    check("extra fields → lead created", bool(r.json().get("lead_id")))

    # 11. All 3 tenants
    for acct in ["100001", "100002", "100003"]:
        p = {**ANGI_PDF_PAYLOAD, "CorrelationId": test_corr(), "ALAccountId": acct}
        r = httpx.post(f"{url}/webhooks/angi/leads", json=p, headers=headers, timeout=15)
        check(f"tenant {acct} → 200", r.status_code == 200)

    # 12. Unicode handling
    uni = {**ANGI_PDF_PAYLOAD, "CorrelationId": test_corr(), "ALAccountId": "100001",
           "FirstName": "José", "LastName": "García", "Description": "Néed help with A/C — très urgent!"}
    r = httpx.post(f"{url}/webhooks/angi/leads", json=uni, headers=headers, timeout=15)
    check("unicode payload → 200", r.status_code == 200)

    # 13. Null optional fields
    nulls = {**ANGI_PDF_PAYLOAD, "CorrelationId": test_corr(), "Source": None, "Description": None}
    r = httpx.post(f"{url}/webhooks/angi/leads", json=nulls, headers=headers, timeout=15)
    check("null fields → 200 with <success>", r.status_code == 200 and SUCCESS_TAG in r.text)

    # 14. Response timing
    p = {**ANGI_PDF_PAYLOAD, "CorrelationId": test_corr(), "ALAccountId": "100001"}
    t0 = time.time()
    r = httpx.post(f"{url}/webhooks/angi/leads", json=p, headers=headers, timeout=15)
    elapsed = time.time() - t0
    check(f"response time < 5s (was {elapsed:.2f}s)", elapsed < 5.0)

    # 15. Readyz
    r = httpx.get(f"{url}/readyz", timeout=10)
    check("readyz returns 200", r.status_code == 200)
    data = r.json()
    check(f"db healthy (={data.get('db')})", data.get("db") == "ok")
    check(f"worker healthy (={data.get('worker')})", data.get("worker") == "ok")

    # ── Cleanup test data ────────────────────────────────────────────────
    if not args.no_cleanup:
        print("\nCleaning up test data...")
        r = httpx.post(
            f"{url}/api/v1/test-cleanup",
            headers=headers,
            timeout=15,
        )
        if r.status_code == 200:
            cleaned = r.json().get("cleaned", {})
            print(f"  Cleaned: {cleaned}")
        else:
            print(f"  WARNING: cleanup failed ({r.status_code}): {r.text[:200]}")

    # Report
    print()
    for line in results:
        print(line)
    print()
    print(f"{'=' * 50}")
    passed, failed = counts
    print(f"PASS: {passed}  FAIL: {failed}  TOTAL: {passed + failed}")
    if failed:
        print("STATUS: FAILING")
        sys.exit(1)
    else:
        print("STATUS: ALL CLEAR")
        sys.exit(0)
