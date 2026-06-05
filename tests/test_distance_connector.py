"""Unit test for the distance connector framework primitive — fully offline
(injected http). Covers: real-distance math, home-geocode caching, and the
fail-closed paths (timeout, 429, empty geocode)."""
from __future__ import annotations

import pytest

from app.connectors.distance import DistanceConnector, DistanceUnavailable

# Canned responses: Nominatim geocodes + an OSRM route (MTV->Berkeley = 73744.3 m).
_GEO = {
    "mountain view": [{"lat": "37.386", "lon": "-122.083"}],
    "berkeley": [{"lat": "37.871", "lon": "-122.272"}],
    "palo alto": [{"lat": "37.442", "lon": "-122.143"}],
}


def _stub_http(url, headers):
    if "search" in url or "nominatim" in url:
        q = urllib_q(url)
        for name, payload in _GEO.items():
            if name in q:
                return payload
        return []
    if "route" in url:
        return {"routes": [{"distance": 73744.3}]}
    raise AssertionError(f"unexpected url {url}")


def urllib_q(url):
    import urllib.parse
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [""])[0].lower()


def _conn(http=_stub_http, **kw):
    kw.setdefault("region_suffix", ", CA, USA")
    kw.setdefault("sleep", lambda s: None)
    return DistanceConnector(http_get=http, **kw)


def test_one_way_miles():
    assert abs(_conn().one_way_miles("Mountain View", "Berkeley") - 45.8) < 0.2


def test_round_trip_miles():
    assert abs(_conn().round_trip_miles("Mountain View", "Berkeley") - 91.6) < 0.3


def test_leg_miles():
    assert abs(_conn().leg_miles("Palo Alto", "Berkeley") - 45.8) < 0.2  # stub routes all to 73744.3 m


def test_home_geocode_is_cached():
    calls = []

    def counting(url, headers):
        calls.append(url)
        return _stub_http(url, headers)

    c = _conn(http=counting)
    c.one_way_miles("Mountain View", "Berkeley")
    c.one_way_miles("Mountain View", "Berkeley")  # geocodes should be cache hits now
    geocode_calls = [u for u in calls if "search" in u]
    assert len(geocode_calls) == 2  # home + dest geocoded once each across two routes


def test_timeout_fails_closed():
    def boom(url, headers):
        raise TimeoutError("timed out")

    with pytest.raises(DistanceUnavailable):
        _conn(http=boom).round_trip_miles("Mountain View", "Berkeley")


def test_http_429_fails_closed():
    def err429(url, headers):
        raise DistanceUnavailable("http 429 for nominatim")

    with pytest.raises(DistanceUnavailable):
        _conn(http=err429).geocode("Berkeley")


def test_empty_geocode_fails_closed():
    with pytest.raises(DistanceUnavailable):
        _conn(http=lambda u, h: []).geocode("Nowheresville XYZ 99999")


def test_from_config_defaults():
    c = DistanceConnector.from_config({"region_suffix": ", CA, USA"})
    assert c.region_suffix == ", CA, USA"
    assert c.geocode_url.startswith("https://")
