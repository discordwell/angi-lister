"""Tests for the monitoring service — error rate, schema drift, volume anomaly, and alerting."""

import datetime as dt
import uuid
from unittest.mock import patch

from app.models import Lead, WebhookReceipt
from app.services import monitoring
from app.services.monitoring import (
    check_error_rate,
    check_schema_drift,
    check_volume_anomaly,
    run_daily_health_check,
    send_alert,
    _should_alert,
    _last_alert_time,
    check_and_alert_parse_failure,
)


def _create_receipt(db, parse_valid=True, schema_drift=None, minutes_ago=0):
    """Helper: create a WebhookReceipt with given attributes."""
    receipt = WebhookReceipt(
        headers={"x-source": "test"},
        raw_body={"test": True},
        auth_valid=True,
        parse_valid=parse_valid,
        schema_drift=schema_drift,
        received_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago),
    )
    db.add(receipt)
    db.flush()
    return receipt


def _create_lead(db, hours_ago=0):
    """Helper: create a Lead at a given time offset."""
    lead = Lead(
        correlation_id=str(uuid.uuid4()),
        al_account_id="100001",
        status="mapped",
        first_name="Test",
        last_name="User",
        email="test@example.com",
        phone="5550000000",
        raw_payload={},
        created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago),
    )
    db.add(lead)
    db.flush()
    return lead


# ---------------------------------------------------------------------------
# check_error_rate
# ---------------------------------------------------------------------------

def test_check_error_rate_below_threshold(db):
    """2 failures (below threshold of 3) should return None."""
    _create_receipt(db, parse_valid=False, minutes_ago=5)
    _create_receipt(db, parse_valid=False, minutes_ago=10)
    db.flush()

    result = check_error_rate(db, window_minutes=60)
    assert result is None


def test_check_error_rate_above_threshold(db):
    """4 failures (above threshold of 3) should return alert dict."""
    for i in range(4):
        _create_receipt(db, parse_valid=False, minutes_ago=i * 5)
    db.flush()

    result = check_error_rate(db, window_minutes=60)
    assert result is not None
    assert result["type"] == "error_rate"
    assert result["count"] == 4
    assert result["threshold"] == 3


def test_check_error_rate_outside_window(db):
    """Failures older than the window should not count."""
    for i in range(5):
        _create_receipt(db, parse_valid=False, minutes_ago=120)  # 2 hours ago
    db.flush()

    result = check_error_rate(db, window_minutes=60)
    assert result is None


def test_check_error_rate_mixed(db):
    """Only parse_valid=False receipts should count."""
    _create_receipt(db, parse_valid=False, minutes_ago=5)
    _create_receipt(db, parse_valid=True, minutes_ago=5)
    _create_receipt(db, parse_valid=False, minutes_ago=10)
    _create_receipt(db, parse_valid=True, minutes_ago=10)
    db.flush()

    result = check_error_rate(db, window_minutes=60)
    assert result is None  # only 2 failures, below threshold of 3


# ---------------------------------------------------------------------------
# check_schema_drift
# ---------------------------------------------------------------------------

def test_check_schema_drift_found(db):
    """Receipts with drift should return aggregated summary."""
    _create_receipt(db, schema_drift={
        "missing_fields": ["Category"],
        "extra_fields": ["NewField"],
    }, minutes_ago=5)
    _create_receipt(db, schema_drift={
        "missing_fields": ["Urgency"],
        "extra_fields": ["NewField", "AnotherField"],
    }, minutes_ago=10)
    db.flush()

    result = check_schema_drift(db, window_hours=1)
    assert result is not None
    assert result["type"] == "schema_drift"
    assert result["receipt_count"] == 2
    assert sorted(result["missing_fields"]) == ["Category", "Urgency"]
    assert sorted(result["extra_fields"]) == ["AnotherField", "NewField"]


def test_check_schema_drift_none(db):
    """No drift receipts should return None."""
    _create_receipt(db, schema_drift=None, minutes_ago=5)
    db.flush()

    result = check_schema_drift(db, window_hours=1)
    assert result is None


def test_check_schema_drift_outside_window(db):
    """Drift receipts older than the window should not count."""
    _create_receipt(db, schema_drift={
        "missing_fields": ["Category"],
    }, minutes_ago=1500)  # 25 hours ago
    db.flush()

    result = check_schema_drift(db, window_hours=24)
    assert result is None


# ---------------------------------------------------------------------------
# check_volume_anomaly
# ---------------------------------------------------------------------------

def test_check_volume_anomaly_triggered(db):
    """Old leads but no recent ones should trigger anomaly."""
    _create_lead(db, hours_ago=24)
    db.flush()

    result = check_volume_anomaly(db, quiet_hours=6)
    assert result is not None
    assert result["type"] == "volume_anomaly"
    assert result["quiet_hours"] == 6


def test_check_volume_anomaly_no_history(db):
    """Empty system should not trigger (no false positive)."""
    result = check_volume_anomaly(db, quiet_hours=6)
    assert result is None


def test_check_volume_anomaly_recent_leads(db):
    """Recent leads exist — no anomaly."""
    _create_lead(db, hours_ago=1)
    db.flush()

    result = check_volume_anomaly(db, quiet_hours=6)
    assert result is None


# ---------------------------------------------------------------------------
# send_alert
# ---------------------------------------------------------------------------

def test_send_alert_no_email_configured(db):
    """With empty ALERT_EMAIL, send_alert should return False."""
    result = send_alert("Test Alert", "Test body")
    assert result is False


# ---------------------------------------------------------------------------
# debounce
# ---------------------------------------------------------------------------

def test_debounce_prevents_rapid_alerts(db):
    """check_and_alert_parse_failure should respect debounce."""
    # Clear debounce state
    _last_alert_time.clear()

    # Create enough failures to trigger
    for i in range(5):
        _create_receipt(db, parse_valid=False, minutes_ago=i)
    db.flush()

    with patch.object(monitoring, "send_alert", return_value=True) as mock_send:
        check_and_alert_parse_failure(db)
        check_and_alert_parse_failure(db)  # second call should be debounced

    # send_alert should only be called once
    assert mock_send.call_count == 1


def test_should_alert_initial():
    """First call for a new alert type should return True."""
    _last_alert_time.pop("test_type", None)
    assert _should_alert("test_type", debounce_minutes=30) is True


# ---------------------------------------------------------------------------
# run_daily_health_check
# ---------------------------------------------------------------------------

def test_daily_health_check_all_clear(db):
    """Clean system should return no issues."""
    results = run_daily_health_check(db)
    assert results["error_rate"] is None
    assert results["schema_drift"] is None
    assert results["volume_anomaly"] is None


def test_daily_health_check_with_issues(db):
    """System with drift should surface it in results."""
    _create_receipt(db, schema_drift={"missing_fields": ["Category"]}, minutes_ago=60)
    db.flush()

    results = run_daily_health_check(db)
    assert results["schema_drift"] is not None
    assert results["schema_drift"]["type"] == "schema_drift"


# ---------------------------------------------------------------------------
# Schema health endpoint
# ---------------------------------------------------------------------------

def test_schema_health_endpoint_ok(client):
    """Clean system should return status=ok."""
    resp = client.get("/api/v1/health/schema")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["schema_drift"] is None
    assert data["error_rate"] is None


def test_schema_health_endpoint_degraded(db, client):
    """System with drift should return status=degraded."""
    _create_receipt(db, schema_drift={"missing_fields": ["Category"]}, minutes_ago=60)
    db.flush()

    resp = client.get("/api/v1/health/schema")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["schema_drift"] is not None
