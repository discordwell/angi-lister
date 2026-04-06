"""Geocoding service using HERE Maps API with postal-code-level caching.

Cache strategy: one row per postal code in geocode_cache table. Postal-code
precision is sufficient for proximity pricing (we compute distances to tenant
home bases, not routing). Cache dramatically improves hit rate since many leads
in the same metro share postal codes.
"""

import datetime as dt
import logging

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import GeocodeCache

log = logging.getLogger(__name__)

HERE_GEOCODE_URL = "https://geocode.search.hereapi.com/v1/geocode"


def geocode_address(
    db: Session,
    address_line1: str | None,
    city: str | None,
    state: str | None,
    postal_code: str | None,
) -> tuple[float, float] | None:
    """Geocode an address to (lat, lng). Checks postal_code cache first.

    Returns None if geocoding fails or no API key is configured.
    """
    if not postal_code:
        return None

    # --- Cache lookup ---------------------------------------------------------
    now = dt.datetime.now(dt.UTC)
    cached = db.query(GeocodeCache).filter(GeocodeCache.postal_code == postal_code).first()
    if cached and cached.expires_at.replace(tzinfo=dt.UTC) > now:
        return (cached.lat, cached.lng)

    # --- HERE API call --------------------------------------------------------
    if not settings.here_api_key:
        log.warning("No HERE_API_KEY configured — skipping geocoding")
        return None

    query_parts = [p for p in [address_line1, city, state, postal_code] if p]
    query = ", ".join(query_parts)

    try:
        resp = httpx.get(
            HERE_GEOCODE_URL,
            params={
                "q": query,
                "apiKey": settings.here_api_key,
                "in": "countryCode:USA",
                "limit": "1",
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            log.warning("HERE returned no results for query=%r", query)
            return None

        position = items[0].get("position", {})
        lat = position.get("lat")
        lng = position.get("lng")
        if lat is None or lng is None:
            return None

        # --- Store in cache ---------------------------------------------------
        ttl = dt.timedelta(days=settings.geocode_cache_ttl_days)
        if cached:
            cached.lat = lat
            cached.lng = lng
            cached.full_address = query
            cached.provider = "here"
            cached.created_at = now
            cached.expires_at = now + ttl
        else:
            db.add(GeocodeCache(
                postal_code=postal_code,
                lat=lat,
                lng=lng,
                full_address=query,
                provider="here",
                created_at=now,
                expires_at=now + ttl,
            ))
        db.flush()

        return (lat, lng)

    except Exception:
        log.exception("HERE geocoding failed for query=%r", query)
        return None
