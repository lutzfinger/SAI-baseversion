"""
Live FX rate lookup, with on-disk cache.

The original fx.py shipped a static USD↔EUR rate of 0.92 — fine for
quick demos but wrong for production invoicing. This module looks up
the historical rate for a specific date via Frankfurter.app (free,
no API key, sourced from ECB). Results are cached on disk so a re-run
of the same trip doesn't re-hit the network.

Cache: ~/Library/Caches/SAI/fx_rates.json
Source: https://api.frankfurter.app  (ECB reference rates, weekdays only)

If the lookup fails (network down, weekend date, missing pair), the
caller can fall back to the static table in fx.py.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

import requests

_CACHE_PATH = Path(os.path.expanduser("~/Library/Caches/SAI/fx_rates.json"))


def _load_cache() -> dict:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def get_rate(from_ccy: str, to_ccy: str, on_date: date) -> float:
    """Return the FX rate from `from_ccy` to `to_ccy` on `on_date`.

    Multiplies an amount in from_ccy by the returned rate to get to_ccy.
    Weekends/holidays fall back to the most recent earlier weekday rate
    (Frankfurter does this automatically).

    Caches every lookup on disk under (date, from, to).
    """
    from_ccy = from_ccy.upper()
    to_ccy = to_ccy.upper()
    if from_ccy == to_ccy:
        return 1.0

    cache = _load_cache()
    key = f"{on_date.isoformat()}|{from_ccy}|{to_ccy}"
    if key in cache:
        return cache[key]

    # Frankfurter takes date in path, base+symbols as query
    # Their server is weekday-only; if `on_date` is a weekend, they
    # automatically respond with the previous weekday's rates.
    url = f"https://api.frankfurter.app/{on_date.isoformat()}"
    r = requests.get(url, params={"from": from_ccy, "to": to_ccy}, timeout=10)
    r.raise_for_status()
    data = r.json()
    rate = data["rates"][to_ccy]

    cache[key] = rate
    _save_cache(cache)
    return rate


def convert(amount: float, from_ccy: str, to_ccy: str, on_date: date) -> tuple[float, float]:
    """Convert `amount` from from_ccy to to_ccy at the rate on `on_date`.

    Returns (converted_amount, rate_used). The rate is included so the
    caller can log it into the audit trail / write it into the invoice
    line description.
    """
    rate = get_rate(from_ccy, to_ccy, on_date)
    return round(amount * rate, 2), rate
