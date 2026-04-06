"""Simulate Angi webhook calls to test the full pipeline end-to-end.

Usage:
    python -m scripts.simulate                          # 1 lead to localhost:8000
    python -m scripts.simulate --url https://angi.discordwell.com --count 5
    python -m scripts.simulate --duplicate               # send same lead twice
    python -m scripts.simulate --bad-auth                # test auth rejection
    python -m scripts.simulate --bad-payload              # test parse failure
    python -m scripts.simulate --unmapped                 # test unmapped account
    python -m scripts.simulate --drift                    # test schema drift detection
"""

import argparse
import json
import random
import string
import sys
import uuid

import httpx


DEFAULT_URL = "https://angi.discordwell.com"
DEFAULT_API_KEY = "test-api-key-change-me"

FIRST_NAMES = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Hank", "Iris", "Jack"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]
CATEGORIES = ["HVAC Repair", "Plumbing", "Electrical", "Water Heater", "AC Installation", "Drain Cleaning"]
URGENCIES = ["Flexible", "Within 48 hours", "Today/Emergency", "Within a week"]
CITIES = [
    ("Indianapolis", "IN", "46201"),
    ("Indianapolis", "IN", "46203"),
    ("Indianapolis", "IN", "46220"),
    ("Carmel", "IN", "46032"),
    ("Fishers", "IN", "46037"),
]
# These map to seeded tenants: Apex HVAC, BlueWave Plumbing, Spark Electric
ACCOUNT_IDS = ["100001", "100002", "100003"]


def random_email(first: str, last: str) -> str:
    suffix = "".join(random.choices(string.digits, k=3))
    domain = random.choice(["gmail.com", "yahoo.com", "outlook.com"])
    return f"{first.lower()}.{last.lower()}{suffix}@{domain}"


def random_phone() -> str:
    return f"({random.randint(200,999)}) {random.randint(100,999)}-{random.randint(1000,9999)}"


def make_lead_payload(
    correlation_id: str | None = None,
    al_account_id: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> dict:
    first = first_name or random.choice(FIRST_NAMES)
    last = last_name or random.choice(LAST_NAMES)
    city, state, postal = random.choice(CITIES)

    return {
        "FirstName": first,
        "LastName": last,
        "PhoneNumber": random_phone(),
        "PostalAddress": {
            "AddressFirstLine": f"{random.randint(100,9999)} {random.choice(['Main', 'Oak', 'Elm', 'Maple', 'Cedar'])} St",
            "AddressSecondLine": random.choice(["", "", "", f"Apt {random.randint(1,20)}"]),
            "City": city,
            "State": state,
            "PostalCode": postal,
        },
        "Email": random_email(first, last),
        "Source": "Angi",
        "Description": f"Need help with {random.choice(CATEGORIES).lower()} at my home. {random.choice(['Urgent!', 'Flexible timing.', 'Please call first.', ''])}",
        "Category": random.choice(CATEGORIES),
        "Urgency": random.choice(URGENCIES),
        "CorrelationId": correlation_id or str(uuid.uuid4()),
        "ALAccountId": al_account_id or random.choice(ACCOUNT_IDS),
    }


def send_lead(url: str, api_key: str, payload: dict) -> httpx.Response:
    headers = {"Content-Type": "application/json", "X-API-KEY": api_key}
    resp = httpx.post(f"{url}/webhooks/angi/leads", json=payload, headers=headers, timeout=15)
    return resp


def run_simulation(args):
    url = args.url.rstrip("/")
    api_key = args.api_key

    print(f"Target: {url}")
    print(f"API Key: {api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else f"API Key: {api_key}")
    print()

    # --- Bad auth test ---
    if args.bad_auth:
        print("=== Bad Auth Test ===")
        payload = make_lead_payload()
        resp = send_lead(url, "WRONG-KEY", payload)
        print(f"  Status: {resp.status_code} (expected 401)")
        print(f"  Body: {resp.text}")
        print()

    # --- Bad payload test ---
    if args.bad_payload:
        print("=== Bad Payload Test ===")
        bad = {"garbage": "data", "CorrelationId": str(uuid.uuid4())}
        resp = send_lead(url, api_key, bad)
        print(f"  Status: {resp.status_code} (expected 200 with parse failure)")
        print(f"  Body: {json.dumps(resp.json(), indent=2)}")
        print()

    # --- Schema drift test ---
    if args.drift:
        print("=== Schema Drift Test ===")
        payload = make_lead_payload()
        payload["UnexpectedNewField"] = "surprise"
        payload["PostalAddress"]["CountryCode"] = "US"
        resp = send_lead(url, api_key, payload)
        print(f"  Status: {resp.status_code}")
        print(f"  Body: {json.dumps(resp.json(), indent=2)}")
        print()

    # --- Unmapped account test ---
    if args.unmapped:
        print("=== Unmapped Account Test ===")
        payload = make_lead_payload(al_account_id="ACC-UNKNOWN-999")
        resp = send_lead(url, api_key, payload)
        print(f"  Status: {resp.status_code}")
        print(f"  Body: {json.dumps(resp.json(), indent=2)}")
        print()

    # --- Duplicate test ---
    if args.duplicate:
        print("=== Duplicate Test ===")
        corr_id = str(uuid.uuid4())
        payload = make_lead_payload(correlation_id=corr_id)
        print(f"  Sending lead (CorrelationId={corr_id})...")
        resp1 = send_lead(url, api_key, payload)
        print(f"  First:  {resp1.status_code} -> {resp1.json().get('lead_id', 'N/A')}")
        resp2 = send_lead(url, api_key, payload)
        print(f"  Second: {resp2.status_code} -> {resp2.json().get('lead_id', 'N/A')} (should be same lead_id)")
        print()

    # --- Normal leads ---
    count = args.count
    if count > 0:
        print(f"=== Sending {count} lead(s) ===")
        for i in range(count):
            payload = make_lead_payload()
            resp = send_lead(url, api_key, payload)
            data = resp.json()
            status_icon = "OK" if resp.status_code == 200 else f"ERR({resp.status_code})"
            lead_id = data.get("lead_id", "N/A")
            name = f"{payload['FirstName']} {payload['LastName']}"
            print(f"  [{i+1}/{count}] {status_icon} | {name} | lead_id={lead_id}")
        print()

    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Simulate Angi webhook calls")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Base URL (default: {DEFAULT_URL})")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key for X-API-KEY header")
    parser.add_argument("--count", type=int, default=1, help="Number of normal leads to send")
    parser.add_argument("--duplicate", action="store_true", help="Send the same lead twice")
    parser.add_argument("--bad-auth", action="store_true", help="Test with wrong API key")
    parser.add_argument("--bad-payload", action="store_true", help="Send malformed payload")
    parser.add_argument("--unmapped", action="store_true", help="Send lead with unknown ALAccountId")
    parser.add_argument("--drift", action="store_true", help="Send lead with extra/unexpected fields")
    parser.add_argument("--all", action="store_true", help="Run all test scenarios")
    args = parser.parse_args()

    if args.all:
        args.bad_auth = True
        args.bad_payload = True
        args.drift = True
        args.unmapped = True
        args.duplicate = True
        if args.count < 3:
            args.count = 3

    run_simulation(args)


if __name__ == "__main__":
    main()
