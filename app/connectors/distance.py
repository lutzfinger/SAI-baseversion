"""Driving-distance connector — a FRAMEWORK PRIMITIVE (PRINCIPLES §33a).

Reusable by ANY skill that needs real road distances between places (trip
mileage, travel ops, meeting logistics, future location skills). It geocodes
place names via Nominatim (OpenStreetMap) and routes via OSRM. Keyless by
default; config-pluggable to a keyed provider later without touching callers.

Design contract:
  - **Fail closed (§6):** any HTTP error, timeout, 429, empty geocode, or
    malformed payload raises ``DistanceUnavailable``. Callers must never act on
    a guessed distance — they either use the number or fail closed too.
  - **Connector isolation:** this is the ONLY place the routing/geocoding HTTP
    lives; skills call these methods, never the network directly.
  - **Polite:** Nominatim asks for ~1 req/s + a real User-Agent; this connector
    caches every geocode in-process (so a fixed home is geocoded once) and
    spaces Nominatim calls by ``geocode_min_interval_s``.
  - **Injectable I/O:** ``http_get`` / ``sleep`` / ``clock`` are injectable so
    the unit test runs fully offline.

Public API:
  - geocode(place) -> (lat, lon)
  - one_way_miles(origin, dest) -> float
  - round_trip_miles(home, place) -> float          # 2 × one-way ("back & forth")
  - leg_miles(a, b) -> float                          # one-way a→b
  - DistanceConnector.from_config(cfg) -> DistanceConnector
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

MILES_PER_METER = 1.0 / 1609.344

DEFAULT_GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
DEFAULT_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving"
DEFAULT_USER_AGENT = "SAI-distance-connector/0.1 (personal)"

# (url, headers) -> parsed JSON (dict or list)
HttpGet = Callable[[str, dict], Any]


class DistanceUnavailable(RuntimeError):
    """Raised on any geocode/route/HTTP/parse failure (fail-closed, §6)."""


def _default_http_get(url: str, headers: dict) -> Any:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise DistanceUnavailable(f"http {status} for {url}")
            return json.loads(resp.read().decode("utf-8"))
    except DistanceUnavailable:
        raise
    except Exception as exc:  # URLError / timeout / HTTPError / JSON / etc.
        raise DistanceUnavailable(f"{type(exc).__name__}: {exc}") from exc


class DistanceConnector:
    def __init__(
        self,
        *,
        http_get: Optional[HttpGet] = None,
        geocode_url: str = DEFAULT_GEOCODE_URL,
        route_url: str = DEFAULT_ROUTE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        region_suffix: str = "",
        geocode_min_interval_s: float = 1.1,
        sleep: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.http_get = http_get or _default_http_get
        self.geocode_url = geocode_url
        self.route_url = route_url
        self.user_agent = user_agent
        self.region_suffix = region_suffix
        self.geocode_min_interval_s = geocode_min_interval_s
        self.sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._geocode_cache: dict[str, tuple[float, float]] = {}
        self._last_geocode_at: float = 0.0

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "DistanceConnector":
        cfg = cfg or {}
        return cls(
            geocode_url=cfg.get("geocode_url", DEFAULT_GEOCODE_URL),
            route_url=cfg.get("route_url", DEFAULT_ROUTE_URL),
            user_agent=cfg.get("user_agent", DEFAULT_USER_AGENT),
            region_suffix=cfg.get("region_suffix", ""),
            geocode_min_interval_s=float(cfg.get("geocode_min_interval_s", 1.1)),
        )

    # --- internal ---------------------------------------------------------

    def _get(self, url: str) -> Any:
        try:
            return self.http_get(url, {"User-Agent": self.user_agent})
        except DistanceUnavailable:
            raise
        except Exception as exc:
            raise DistanceUnavailable(f"http error: {type(exc).__name__}: {exc}") from exc

    # --- public -----------------------------------------------------------

    def geocode(self, place: str) -> tuple[float, float]:
        key = str(place).strip().lower()
        if not key:
            raise DistanceUnavailable("empty place")
        if key in self._geocode_cache:
            return self._geocode_cache[key]
        # Polite spacing for Nominatim (~1 req/s).
        if self._last_geocode_at:
            elapsed = self._clock() - self._last_geocode_at
            if elapsed < self.geocode_min_interval_s:
                self.sleep(self.geocode_min_interval_s - elapsed)
        query = f"{place}{self.region_suffix}" if self.region_suffix else str(place)
        url = f"{self.geocode_url}?{urllib.parse.urlencode({'q': query, 'format': 'json', 'limit': 1})}"
        data = self._get(url)
        self._last_geocode_at = self._clock()
        if not isinstance(data, list) or not data:
            raise DistanceUnavailable(f"no geocode result for {place!r}")
        try:
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise DistanceUnavailable(f"bad geocode payload for {place!r}: {exc}") from exc
        self._geocode_cache[key] = (lat, lon)
        return (lat, lon)

    def one_way_miles(self, origin: str, dest: str) -> float:
        olat, olon = self.geocode(origin)
        dlat, dlon = self.geocode(dest)
        coords = f"{olon},{olat};{dlon},{dlat}"  # OSRM wants lon,lat
        url = f"{self.route_url}/{coords}?overview=false"
        data = self._get(url)
        try:
            meters = float(data["routes"][0]["distance"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise DistanceUnavailable(f"bad route payload {origin!r}->{dest!r}: {exc}") from exc
        return round(meters * MILES_PER_METER, 1)

    def round_trip_miles(self, home: str, place: str) -> float:
        return round(self.one_way_miles(home, place) * 2.0, 1)

    def leg_miles(self, a: str, b: str) -> float:
        return self.one_way_miles(a, b)
