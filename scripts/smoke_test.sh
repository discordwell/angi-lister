#!/usr/bin/env bash
# smoke_test.sh — Quick smoke test against a running angi-lister instance.
# Usage: ./scripts/smoke_test.sh [BASE_URL]
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
API_KEY="${ANGI_API_KEY:-test-api-key-change-me}"
PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
check() {
    local desc="$1" expected="$2" actual="$3"
    if [ "$actual" = "$expected" ]; then
        pass "$desc"
    else
        fail "$desc (expected=$expected, got=$actual)"
    fi
}

echo "=== Smoke Test: ${BASE_URL} ==="
echo ""

# --- 1. Health check ---
echo "[1/6] Health check"
STATUS=$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/healthz")
check "/healthz returns 200" "200" "$STATUS"

BODY=$(curl -s "${BASE_URL}/healthz")
echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['status']=='ok'" 2>/dev/null \
    && pass "/healthz status=ok" \
    || fail "/healthz status != ok"

# --- 2. Readiness check ---
echo ""
echo "[2/6] Readiness check"
STATUS=$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/readyz")
check "/readyz returns 200" "200" "$STATUS"

# --- 3. Auth rejection ---
echo ""
echo "[3/6] Auth rejection"
STATUS=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "${BASE_URL}/webhooks/angi/leads" \
    -H "Content-Type: application/json" \
    -H "X-API-KEY: WRONG-KEY" \
    -d '{"test": true}')
check "Wrong API key returns 401" "401" "$STATUS"

# --- 4. Valid lead ingestion ---
echo ""
echo "[4/6] Valid lead ingestion"
CORR_ID="smoke-test-$(date +%s)-$(( RANDOM ))"
PAYLOAD=$(cat <<EOF
{
    "FirstName": "Smoke",
    "LastName": "Test",
    "PhoneNumber": "(555) 999-0001",
    "PostalAddress": {
        "AddressFirstLine": "123 Test St",
        "AddressSecondLine": "",
        "City": "New York",
        "State": "NY",
        "PostalCode": "10001"
    },
    "Email": "smoke.test@example.com",
    "Source": "Angi",
    "Description": "Smoke test lead",
    "Category": "HVAC Repair",
    "Urgency": "Flexible",
    "CorrelationId": "${CORR_ID}",
    "ALAccountId": "ACC-001"
}
EOF
)

RESP=$(curl -s -w '\n%{http_code}' \
    -X POST "${BASE_URL}/webhooks/angi/leads" \
    -H "Content-Type: application/json" \
    -H "X-API-KEY: ${API_KEY}" \
    -d "${PAYLOAD}")

HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)

check "Lead POST returns 200" "200" "$HTTP_CODE"
echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('lead_id')" 2>/dev/null \
    && pass "Response contains lead_id" \
    || fail "Response missing lead_id"

# --- 5. Idempotency ---
echo ""
echo "[5/6] Idempotency (same CorrelationId)"
RESP2=$(curl -s -w '\n%{http_code}' \
    -X POST "${BASE_URL}/webhooks/angi/leads" \
    -H "Content-Type: application/json" \
    -H "X-API-KEY: ${API_KEY}" \
    -d "${PAYLOAD}")

HTTP_CODE2=$(echo "$RESP2" | tail -1)
BODY2=$(echo "$RESP2" | head -n -1)

check "Idempotent POST returns 200" "200" "$HTTP_CODE2"

LEAD_ID_1=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('lead_id',''))" 2>/dev/null || echo "")
LEAD_ID_2=$(echo "$BODY2" | python3 -c "import sys,json; print(json.load(sys.stdin).get('lead_id',''))" 2>/dev/null || echo "")

if [ -n "$LEAD_ID_1" ] && [ "$LEAD_ID_1" = "$LEAD_ID_2" ]; then
    pass "Same lead_id returned on retry"
else
    fail "Different lead_id on retry (${LEAD_ID_1} vs ${LEAD_ID_2})"
fi

# --- 6. Parse failure (bad payload) ---
echo ""
echo "[6/6] Parse failure handling"
BAD_RESP=$(curl -s -w '\n%{http_code}' \
    -X POST "${BASE_URL}/webhooks/angi/leads" \
    -H "Content-Type: application/json" \
    -H "X-API-KEY: ${API_KEY}" \
    -d '{"garbage": "data", "CorrelationId": "bad-parse-test"}')

BAD_CODE=$(echo "$BAD_RESP" | tail -1)
check "Bad payload returns 200 (receipt recorded)" "200" "$BAD_CODE"

# --- Summary ---
echo ""
echo "=============================="
TOTAL=$((PASS + FAIL))
echo "Results: ${PASS}/${TOTAL} passed"
if [ "$FAIL" -gt 0 ]; then
    echo "FAILURES: ${FAIL}"
    exit 1
else
    echo "All tests passed!"
    exit 0
fi
