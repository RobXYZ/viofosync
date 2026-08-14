"""Reverse geocoding against Nominatim with SQLite caching.

Nominatim's usage policy is 1 request/sec max and requires a
contactable User-Agent (and ideally an email query parameter).
All lookups pass through the cache first; a miss queues on a
shared lock so concurrent requests don't burst the endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Optional

from ..db import Database
from ..settings import SettingsProvider

log = logging.getLogger("viofosync.geocode")

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = (
    "viofosync/0.1 "
    "(https://github.com/RobXYZ/viofosync)"
)
_MIN_INTERVAL_S = 1.05  # spacing between successive requests
_CACHE_RESOLUTION = 3   # round lat/lon to N decimals (≈ 111 m)


def _round(coord: float) -> float:
    return round(coord, _CACHE_RESOLUTION)


def _format_label(data: dict) -> Optional[str]:
    """Pick a short, human-friendly label from Nominatim's
    response. Prefers a street-level identifier plus a town."""
    addr = (data or {}).get("address") or {}
    street = (
        addr.get("road")
        or addr.get("pedestrian")
        or addr.get("residential")
        or addr.get("suburb")
        or addr.get("neighbourhood")
        or addr.get("hamlet")
    )
    town = (
        addr.get("town")
        or addr.get("city")
        or addr.get("village")
        or addr.get("municipality")
        or addr.get("county")
    )
    if street and town and street != town:
        return f"{street}, {town}"
    return street or town or data.get("display_name")


class GeocodeService:
    def __init__(
        self,
        db: Database,
        provider: SettingsProvider,
    ) -> None:
        self.db = db
        self._provider = provider
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    @property
    def email(self) -> Optional[str]:
        return self._provider.get().nominatim_email or None

    @property
    def enabled(self) -> bool:
        return self._provider.get().geocode_enabled

    # --- cache ---

    def cache_lookup(
        self, lat: float, lon: float,
    ) -> Optional[str]:
        """Synchronous cache check only — no network. Used by
        endpoints that want to include whatever's already known
        without waiting."""
        key_lat = _round(lat)
        key_lon = _round(lon)
        with self.db.conn() as c:
            row = c.execute(
                "SELECT label FROM geocode_cache "
                "WHERE lat_key=? AND lon_key=?",
                (key_lat, key_lon),
            ).fetchone()
        return row["label"] if row else None

    def _cache_store(
        self, lat: float, lon: float, label: str,
    ) -> None:
        key_lat = _round(lat)
        key_lon = _round(lon)
        with self.db.write() as c:
            c.execute(
                "INSERT OR REPLACE INTO geocode_cache "
                "(lat_key, lon_key, label, fetched_at) "
                "VALUES (?, ?, ?, ?)",
                (key_lat, key_lon, label, int(time.time())),
            )

    # --- lookup ---

    async def reverse(
        self, lat: float, lon: float,
    ) -> Optional[str]:
        snap = self._provider.get()
        cached = self.cache_lookup(lat, lon)
        if cached:
            return cached
        if not snap.geocode_enabled:
            return None

        # Rate limit + serialise concurrent callers. Cheap:
        # virtually every post-first-hit request hits the cache.
        async with self._lock:
            # Someone may have populated while we queued.
            cached = self.cache_lookup(lat, lon)
            if cached:
                return cached

            wait = _MIN_INTERVAL_S - (
                time.monotonic() - self._last_request
            )
            if wait > 0:
                await asyncio.sleep(wait)
            label = await self._fetch(lat, lon, snap.nominatim_email or None)
            self._last_request = time.monotonic()

        if label:
            self._cache_store(lat, lon, label)
        return label

    async def _fetch(
        self, lat: float, lon: float, email: Optional[str] = None,
    ) -> Optional[str]:
        params = {
            "format": "jsonv2",
            "lat": f"{lat:.6f}",
            "lon": f"{lon:.6f}",
            "zoom": "14",
            "addressdetails": "1",
        }
        if email:
            params["email"] = email
        url = _NOMINATIM_URL + "?" + urllib.parse.urlencode(params)

        def _sync():
            req = urllib.request.Request(
                url, headers={"User-Agent": _USER_AGENT}
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:  # pragma: no cover
                # Coordinates must never enter app_log — it feeds the
                # public debug bundle (which promises "no location
                # data"). Log only the error, not lat/lon.
                log.warning("reverse geocode failed: %s", e)
                return None

        data = await asyncio.get_running_loop().run_in_executor(
            None, _sync,
        )
        if not data:
            return None
        return _format_label(data)
