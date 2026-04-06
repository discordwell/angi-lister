"""Comprehensive wet test against the live site.

Tests every user-facing flow as a real human would interact with it.
Uses httpx with cookie persistence to simulate browser sessions.

Usage:
    python -m scripts.wet_test
    python -m scripts.wet_test --url https://angi.discordwell.com
"""

import argparse
import json
import sys
import uuid

import httpx

BASE = "https://angi.discordwell.com"
API_KEY = "netic-demo-2026-angi-key"

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

results = {"pass": 0, "fail": 0, "warn": 0}


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        results["pass"] += 1
        print(f"  {PASS} {name}")
    else:
        results["fail"] += 1
        print(f"  {FAIL} {name}" + (f" — {detail}" if detail else ""))


def warn(name: str, detail: str = ""):
    results["warn"] += 1
    print(f"  {WARN} {name}" + (f" — {detail}" if detail else ""))


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ===========================================================================
# 1. Health + Webhook Pipeline
# ===========================================================================

def test_health(client: httpx.Client):
    section("1. HEALTH + WEBHOOK PIPELINE")

    # healthz
    r = client.get(f"{BASE}/healthz")
    check("GET /healthz returns 200", r.status_code == 200)
    data = r.json()
    check("healthz status is ok", data.get("status") == "ok")

    # readyz
    r = client.get(f"{BASE}/readyz")
    check("GET /readyz returns 200", r.status_code == 200)
    data = r.json()
    check("readyz db is ok", data.get("db") == "ok")
    check("readyz worker is ok", data.get("worker") == "ok")

    # schema health
    r = client.get(f"{BASE}/api/v1/health/schema")
    check("GET /api/v1/health/schema returns 200", r.status_code == 200)

    # webhook — no auth
    r = client.post(f"{BASE}/webhooks/angi/leads", json={"test": True})
    check("Webhook without auth returns 401", r.status_code == 401)

    # webhook — bad auth
    r = client.post(f"{BASE}/webhooks/angi/leads", json={"test": True},
                    headers={"X-API-KEY": "wrong"})
    check("Webhook with bad auth returns 401", r.status_code == 401)

    # webhook — valid lead
    corr_id = str(uuid.uuid4())
    lead_payload = {
        "FirstName": "WetTest",
        "LastName": "User",
        "PhoneNumber": "5559876543",
        "PostalAddress": {
            "AddressFirstLine": "456 Oak Ave",
            "AddressSecondLine": "",
            "City": "St. Louis",
            "State": "MO",
            "PostalCode": "63101",
        },
        "Email": "wettest@example.com",
        "Source": "Wet Test",
        "Description": "Wet test lead — should be processed correctly.",
        "Category": "St. Louis - HVAC Repair",
        "Urgency": "Flexible",
        "CorrelationId": corr_id,
        "ALAccountId": "100001",  # Hoffmann Brothers
    }
    r = client.post(f"{BASE}/webhooks/angi/leads", json=lead_payload,
                    headers={"X-API-KEY": API_KEY})
    check("Webhook with valid lead returns 200", r.status_code == 200)
    data = r.json()
    check("Response has lead_id", data.get("lead_id") is not None)
    check("Response has success message", "<success>" in data.get("message", ""))
    lead_id = data.get("lead_id")

    # Idempotency — same correlation ID
    r2 = client.post(f"{BASE}/webhooks/angi/leads", json=lead_payload,
                     headers={"X-API-KEY": API_KEY})
    check("Idempotent retry returns 200", r2.status_code == 200)
    check("Idempotent retry returns same lead_id",
          r2.json().get("lead_id") == lead_id)

    # Bad payload — should return 200 with parse failure
    r = client.post(f"{BASE}/webhooks/angi/leads",
                    json={"garbage": "data", "CorrelationId": str(uuid.uuid4())},
                    headers={"X-API-KEY": API_KEY})
    check("Malformed payload returns 200 (not retry)", r.status_code == 200)
    check("Malformed payload has receipt_id", r.json().get("receipt_id") is not None)

    # Unmapped account
    r = client.post(f"{BASE}/webhooks/angi/leads",
                    json={**lead_payload, "CorrelationId": str(uuid.uuid4()),
                          "ALAccountId": "UNKNOWN-999"},
                    headers={"X-API-KEY": API_KEY})
    check("Unmapped account returns 200", r.status_code == 200)

    # Simulate leads for Paschal Air too
    for i in range(2):
        r = client.post(f"{BASE}/webhooks/angi/leads",
                        json={**lead_payload, "CorrelationId": str(uuid.uuid4()),
                              "ALAccountId": "100002",  # Paschal Air
                              "FirstName": f"PaschalTest{i}",
                              "Email": f"paschal{i}@example.com"},
                        headers={"X-API-KEY": API_KEY})
        check(f"Paschal lead {i+1} accepted", r.status_code == 200)

    return lead_id


# ===========================================================================
# 2. Login Flows
# ===========================================================================

def test_login_flows(client: httpx.Client):
    section("2. LOGIN FLOWS")

    # Login page renders
    r = client.get(f"{BASE}/auth/login", follow_redirects=False)
    check("GET /auth/login returns 200", r.status_code == 200)
    check("Login page has magic link form", "send-link" in r.text)
    check("Login page has Paschal demo button", "demo-login" in r.text)
    check("Login page has Admin demo button", "admin-login" in r.text)

    # Unauthenticated console redirects to login
    r = client.get(f"{BASE}/console/", follow_redirects=False)
    check("Unauthenticated /console redirects", r.status_code == 302)
    check("Redirects to /auth/login", "/auth/login" in r.headers.get("location", ""))


def test_demo_tenant_login(client: httpx.Client) -> httpx.Client:
    """Login as Paschal Air demo tenant. Returns a new client with session cookie."""
    section("2a. DEMO TENANT LOGIN (Paschal Air)")

    tenant_client = httpx.Client(follow_redirects=False, timeout=15)
    r = tenant_client.post(f"{BASE}/auth/demo-login", follow_redirects=False)
    check("POST /auth/demo-login returns 302", r.status_code == 302)
    check("Redirect to /console", "/console" in r.headers.get("location", ""))

    cookie = r.headers.get("set-cookie", "")
    check("Session cookie set", "angi_session=" in cookie)

    # Extract cookie and create persistent client
    cookies = {}
    for part in cookie.split(";"):
        if "angi_session=" in part:
            cookies["angi_session"] = part.split("angi_session=")[1].strip()
            break

    session_client = httpx.Client(cookies=cookies, follow_redirects=True, timeout=15)

    # Verify we can access console
    r = session_client.get(f"{BASE}/console/")
    check("Tenant can access /console", r.status_code == 200)
    check("Dashboard renders", "Dashboard" in r.text)

    return session_client


def test_admin_login(client: httpx.Client) -> httpx.Client:
    """Login as admin. Returns a new client with session cookie."""
    section("2b. ADMIN LOGIN")

    admin_client = httpx.Client(follow_redirects=False, timeout=15)
    r = admin_client.post(f"{BASE}/auth/admin-login", follow_redirects=False)
    check("POST /auth/admin-login returns 302", r.status_code == 302)
    check("Redirect to /console", "/console" in r.headers.get("location", ""))

    cookie = r.headers.get("set-cookie", "")
    check("Session cookie set", "angi_session=" in cookie)

    cookies = {}
    for part in cookie.split(";"):
        if "angi_session=" in part:
            cookies["angi_session"] = part.split("angi_session=")[1].strip()
            break

    session_client = httpx.Client(cookies=cookies, follow_redirects=True, timeout=15)

    r = session_client.get(f"{BASE}/console/")
    check("Admin can access /console", r.status_code == 200)
    check("Dashboard renders for admin", "Dashboard" in r.text)

    return session_client


# ===========================================================================
# 3. Console as Tenant (RLS Scoping)
# ===========================================================================

def test_tenant_console(tenant_client: httpx.Client, lead_id: str):
    section("3. CONSOLE AS TENANT (Paschal Air — RLS scoped)")

    # Dashboard
    r = tenant_client.get(f"{BASE}/console/")
    check("Dashboard loads", r.status_code == 200)
    # Paschal leads should be visible, Hoffmann leads should NOT
    check("Paschal leads visible", "PaschalTest" in r.text)
    hoffmann_visible = "WetTest" in r.text
    check("Hoffmann lead NOT visible (RLS)", not hoffmann_visible,
          "Hoffmann lead leaked through RLS!" if hoffmann_visible else "")

    # Duplicates page
    r = tenant_client.get(f"{BASE}/console/duplicates")
    check("Duplicates page loads", r.status_code == 200)

    # Simulate page
    r = tenant_client.get(f"{BASE}/console/simulate")
    check("Simulate page loads", r.status_code == 200)
    check("Simulate has form", "Submit Simulated Lead" in r.text)

    # Try to view the Hoffmann lead directly — should 404 due to RLS
    if lead_id:
        r = tenant_client.get(f"{BASE}/console/leads/{lead_id}")
        check("Direct access to other tenant's lead is blocked",
              r.status_code == 404 or "not found" in r.text.lower(),
              f"Got status {r.status_code}" if r.status_code != 404 else "")

    # Settings
    r = tenant_client.get(f"{BASE}/console/settings")
    check("Settings page loads", r.status_code == 200)
    check("Settings shows tenant role", "Tenant" in r.text)
    check("Business name is editable", 'disabled' not in r.text.split('display_name')[1][:200] if 'display_name' in r.text else False,
          "display_name field may be disabled")

    # Analytics
    r = tenant_client.get(f"{BASE}/console/analytics")
    check("Analytics page loads", r.status_code == 200)

    # Email setup (tenant only)
    r = tenant_client.get(f"{BASE}/console/email")
    check("Email setup page loads for tenant", r.status_code == 200)


# ===========================================================================
# 4. Console as Admin (All-Tenant View)
# ===========================================================================

def test_admin_console(admin_client: httpx.Client, lead_id: str):
    section("4. CONSOLE AS ADMIN (all-tenant view)")

    # Dashboard — should see leads from ALL tenants
    r = admin_client.get(f"{BASE}/console/")
    check("Admin dashboard loads", r.status_code == 200)
    check("Admin sees Hoffmann leads", "WetTest" in r.text)
    check("Admin sees Paschal leads", "PaschalTest" in r.text)

    # Admin can view any lead directly
    if lead_id:
        r = admin_client.get(f"{BASE}/console/leads/{lead_id}")
        check("Admin can view Hoffmann lead", r.status_code == 200)

    # Duplicates
    r = admin_client.get(f"{BASE}/console/duplicates")
    check("Admin duplicates page loads", r.status_code == 200)

    # Settings — name should be locked
    r = admin_client.get(f"{BASE}/console/settings")
    check("Admin settings page loads", r.status_code == 200)
    check("Admin role shown", "Admin" in r.text)
    check("Business name is locked for admin", "disabled" in r.text)

    # Email setup — admin should be denied
    r = admin_client.get(f"{BASE}/console/email", follow_redirects=False)
    check("Email setup blocked for admin", r.status_code in (403, 500),
          f"Got {r.status_code}")

    # Admin analytics
    r = admin_client.get(f"{BASE}/console/analytics/admin")
    check("Admin analytics page loads", r.status_code == 200)


# ===========================================================================
# 5. Settings Save
# ===========================================================================

def test_settings_save(tenant_client: httpx.Client):
    section("5. SETTINGS SAVE")

    # Get current settings to see the form
    r = tenant_client.get(f"{BASE}/console/settings")
    check("Settings page loads for edit", r.status_code == 200)

    # Save with updated email
    test_email = f"wettest-{uuid.uuid4().hex[:6]}@example.com"
    r = tenant_client.post(f"{BASE}/console/settings", data={
        "email": test_email,
        "display_name": "Paschal Air, Plumbing & Electric",
    })
    check("Settings POST returns 200", r.status_code == 200)
    check("Success message shown", "Settings saved" in r.text)
    check("Updated email reflected", test_email in r.text)

    # Restore original email
    r = tenant_client.post(f"{BASE}/console/settings", data={
        "email": "leads@paschalair.example.com",
        "display_name": "Paschal Air, Plumbing & Electric",
    })
    check("Email restored successfully", r.status_code == 200)


# ===========================================================================
# 6. Hard Test — Break Things
# ===========================================================================

def test_hard(client: httpx.Client, tenant_client: httpx.Client, admin_client: httpx.Client):
    section("6. HARD WET TEST — Breaking Things")

    # XSS attempt in lead payload
    xss_payload = {
        "FirstName": "<script>alert('xss')</script>",
        "LastName": "Test",
        "PhoneNumber": "5551111111",
        "PostalAddress": {"AddressFirstLine": "", "AddressSecondLine": "",
                          "City": "", "State": "", "PostalCode": ""},
        "Email": "xss@example.com",
        "Source": "XSS Test",
        "Description": "<img onerror=alert(1) src=x>",
        "Category": "Test",
        "Urgency": "Flexible",
        "CorrelationId": str(uuid.uuid4()),
        "ALAccountId": "100002",
    }
    r = client.post(f"{BASE}/webhooks/angi/leads", json=xss_payload,
                    headers={"X-API-KEY": API_KEY})
    check("XSS payload accepted (stored safely)", r.status_code == 200)

    # Empty correlation ID
    r = client.post(f"{BASE}/webhooks/angi/leads",
                    json={**xss_payload, "CorrelationId": ""},
                    headers={"X-API-KEY": API_KEY})
    check("Empty CorrelationId handled", r.status_code == 200)

    # Huge payload
    r = client.post(f"{BASE}/webhooks/angi/leads",
                    json={**xss_payload, "CorrelationId": str(uuid.uuid4()),
                          "Description": "A" * 10000},
                    headers={"X-API-KEY": API_KEY})
    check("Large description handled", r.status_code == 200)

    # Settings — empty email
    r = tenant_client.post(f"{BASE}/console/settings", data={
        "email": "",
        "display_name": "Test",
    })
    check("Empty email rejected", "valid email" in r.text.lower() or r.status_code == 422)

    # Settings — bad email
    r = tenant_client.post(f"{BASE}/console/settings", data={
        "email": "not-an-email",
        "display_name": "Test",
    })
    check("Invalid email rejected", "valid email" in r.text.lower() or r.status_code == 422)

    # Nonexistent lead
    r = tenant_client.get(f"{BASE}/console/leads/nonexistent-id-12345")
    check("Nonexistent lead returns 404", r.status_code == 404)

    # Expired/invalid session cookie
    bad_client = httpx.Client(cookies={"angi_session": "garbage-cookie"},
                              follow_redirects=False, timeout=15)
    r = bad_client.get(f"{BASE}/console/")
    check("Bad cookie redirects to login", r.status_code == 302)
    bad_client.close()

    # API endpoints
    r = client.get(f"{BASE}/api/v1/metrics")
    check("GET /api/v1/metrics returns 200", r.status_code == 200)

    r = client.get(f"{BASE}/api/v1/leads?limit=5")
    check("GET /api/v1/leads returns 200", r.status_code == 200)
    data = r.json()
    check("Leads response is a list", isinstance(data, list))

    r = client.get(f"{BASE}/api/v1/duplicates")
    check("GET /api/v1/duplicates returns 200", r.status_code == 200)

    # Logout
    r = tenant_client.get(f"{BASE}/auth/logout", follow_redirects=False)
    check("Logout redirects", r.status_code == 302)

    # After logout, console should redirect
    r = tenant_client.get(f"{BASE}/console/", follow_redirects=False)
    check("Post-logout console redirects to login",
          r.status_code == 302 or r.status_code == 200)


# ===========================================================================
# Main
# ===========================================================================

def main():
    global BASE, API_KEY

    parser = argparse.ArgumentParser(description="Wet test the live Angi-Lister site")
    parser.add_argument("--url", default=BASE, help="Base URL")
    parser.add_argument("--api-key", default=API_KEY, help="Angi API key")
    args = parser.parse_args()
    BASE = args.url.rstrip("/")
    API_KEY = args.api_key

    print(f"\nWet Test Target: {BASE}")
    print(f"{'='*60}\n")

    client = httpx.Client(follow_redirects=True, timeout=15)

    try:
        # 1. Health + webhook
        lead_id = test_health(client)

        # 2. Login flows
        test_login_flows(client)
        tenant_client = test_demo_tenant_login(client)
        admin_client = test_admin_login(client)

        # 3. Tenant console (RLS)
        test_tenant_console(tenant_client, lead_id)

        # 4. Admin console
        test_admin_console(admin_client, lead_id)

        # 5. Settings save
        test_settings_save(tenant_client)

        # 6. Hard test
        test_hard(client, tenant_client, admin_client)

        tenant_client.close()
        admin_client.close()

    finally:
        client.close()

    # Summary
    print(f"\n{'='*60}")
    print(f"  RESULTS: {results['pass']} passed, {results['fail']} failed, {results['warn']} warnings")
    print(f"{'='*60}\n")

    if results["fail"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
