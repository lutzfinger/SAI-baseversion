"""Email intake for the headless trip-mileage daemon (deterministic skill code).

Fail-closed sender validation: a trigger thread is acted on ONLY when the
request email is from an allowlisted operator address. Extracts the trip
utterance from subject/snippet/body. No network, no side effects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

_ADDR_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TRIP_RE = re.compile(r"\b(went|going|go|drove|drive|driving|trip|travel|was at|visited)\b", re.I)


@dataclass
class TripRequest:
    utterance: str
    subject: str = ""


def _extract_addr(value: str) -> str:
    m = _ADDR_RE.search(str(value or ""))
    return m.group(0).lower() if m else ""


def parse_trigger_email(meta: dict, *, operator_addresses: Iterable[str]) -> Optional[TripRequest]:
    """Return a TripRequest ONLY if (a) the sender is an allowlisted operator
    address and (b) the email contains a trip statement. Else None (ignored)."""
    sender = _extract_addr(meta.get("from", ""))
    allow = {str(a).strip().lower() for a in (operator_addresses or []) if str(a).strip()}
    if not sender or sender not in allow:
        return None  # fail-closed: never act on a non-operator sender
    text = " ".join(str(meta.get(k, "")) for k in ("subject", "snippet", "body")).strip()
    if not text or not _TRIP_RE.search(text):
        return None
    return TripRequest(utterance=text, subject=str(meta.get("subject", "")))
