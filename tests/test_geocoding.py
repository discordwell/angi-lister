"""Tests for geocoding service with postal code caching."""

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.models import GeocodeCache
from app.services.geocoding import geocode_address


class TestGeocodingCache:
    def test_cache_hit_returns_coords(self, db: Session):
        db.add(GeocodeCache(
            postal_code="46201",
            lat=39.7800,
            lng=-86.1500,
            provider="here",
            created_at=dt.datetime.now(dt.UTC),
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=90),
        ))
        db.flush()

        result = geocode_address(db, "123 Main St", "Indianapolis", "IN", "46201")
        assert result is not None
        assert result == pytest.approx((39.78, -86.15), abs=0.01)

    def test_expired_cache_skipped(self, db: Session, monkeypatch):
        db.add(GeocodeCache(
            postal_code="46201",
            lat=39.7800,
            lng=-86.1500,
            provider="here",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=100),
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=10),
        ))
        db.flush()

        # No HERE API key — so expired cache + no API = None
        monkeypatch.setattr("app.services.geocoding.settings.here_api_key", "")
        result = geocode_address(db, "123 Main St", "Indianapolis", "IN", "46201")
        assert result is None

    def test_no_postal_code_returns_none(self, db: Session):
        result = geocode_address(db, "123 Main St", "Indianapolis", "IN", None)
        assert result is None

    def test_no_api_key_returns_none(self, db: Session, monkeypatch):
        monkeypatch.setattr("app.services.geocoding.settings.here_api_key", "")
        result = geocode_address(db, "123 Main St", "Indianapolis", "IN", "46201")
        assert result is None
