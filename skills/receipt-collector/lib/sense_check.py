"""
Sense-check every billable item against the trip it's claimed against.

Catches the class of mis-tag where an expense is attached to a trip
whose dates don't fit (e.g., a rideshare 26 days before the trip
window probably belongs to a different trip).

Strategy:

  1. **Deterministic gate** (fast, free):
     - Inside trip window  → YES
     - Outside but within an airline/hotel pre-booking range → MAYBE
     - Outside everything                                    → NO

  2. **Local-LLM gate** (cheap; runs on the operator's laptop via Ollama)
     only fires when the deterministic gate says MAYBE. It judges whether
     a pre-trip purchase (e.g., a flight booked 3 months ahead) is
     plausibly *for this trip* given the vendor + description + customer.
     Uses llama3.2:1b by default — sub-second, free, fully local.

Falling order: deterministic → local LLM → operator.

If Ollama isn't available the local-LLM gate is skipped and MAYBE items
are surfaced for operator review unchanged.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Optional


class Verdict(str, Enum):
    YES = "YES"      # clearly part of this trip
    MAYBE = "MAYBE"  # ambiguous; needs operator eye
    NO = "NO"        # not this trip


@dataclass
class Check:
    purchase_id: str
    txn_date: Optional[date]
    vendor: str
    description: str
    amount: float
    verdict: Verdict
    source: str   # "deterministic" or "llm:<model>"
    reason: str


# Generic vendor-category patterns. Operator-specific brand names
# (the airlines and hotels you actually use) come from the overlay
# config — see `extra_airline_hints` / `extra_hotel_hints` args below.
# This keeps the base skill free of any operator's vendor list.
_GENERIC_AIRLINE_RE = r"airline|airways|airlines|flight|carrier"
_GENERIC_HOTEL_RE = r"hotel|inn|resort|hostel|airbnb|lodging|residence|guest house"


def _compile_hint_re(generic_pattern: str, extra_words: list[str] | None) -> re.Pattern:
    pat = generic_pattern
    if extra_words:
        words = "|".join(re.escape(w) for w in extra_words if w)
        if words:
            pat = pat + "|" + words
    return re.compile(pat, re.IGNORECASE)


def deterministic_check(
    txn_date: date,
    vendor: str,
    description: str,
    trip_start: date,
    trip_end: date,
    pre_buffer_days_default: int = 3,
    pre_buffer_days_airline: int = 180,
    pre_buffer_days_hotel: int = 90,
    post_buffer_days: int = 3,
    extra_airline_hints: list[str] | None = None,
    extra_hotel_hints: list[str] | None = None,
) -> tuple[Verdict, str]:
    """First-pass classification on date + vendor only.

    Inside trip window: YES, regardless of vendor.
    Outside on the post side (after trip_end + buffer): NO.
    Outside on the pre side: YES if airline within 180d, hotel within 90d,
    other within 3d; else MAYBE if within a wider 30d, else NO.
    """
    if trip_start <= txn_date <= trip_end:
        return Verdict.YES, f"within trip window {trip_start}..{trip_end}"
    if txn_date > trip_end + timedelta(days=post_buffer_days):
        days = (txn_date - trip_end).days
        return Verdict.NO, f"{days} days AFTER trip ended ({trip_end})"

    # txn_date is BEFORE trip_start
    days_before = (trip_start - txn_date).days
    text = f"{vendor} {description}".lower()
    airline_re = _compile_hint_re(_GENERIC_AIRLINE_RE, extra_airline_hints)
    hotel_re = _compile_hint_re(_GENERIC_HOTEL_RE, extra_hotel_hints)
    is_airline = bool(airline_re.search(text))
    is_hotel = bool(hotel_re.search(text))

    if is_airline and days_before <= pre_buffer_days_airline:
        return Verdict.YES, f"airline ticket booked {days_before}d before trip — plausible"
    if is_hotel and days_before <= pre_buffer_days_hotel:
        return Verdict.YES, f"hotel booking {days_before}d before trip — plausible"
    if days_before <= pre_buffer_days_default:
        return Verdict.YES, f"{days_before}d before trip — within normal buffer"
    if days_before <= 30:
        return Verdict.MAYBE, (
            f"{days_before}d before trip ({txn_date}) — outside normal buffer, "
            f"not an obvious airline/hotel pre-booking. Needs review."
        )
    return Verdict.NO, (
        f"{days_before}d before trip ({txn_date}) — too far outside the window "
        f"and not a flight/hotel pre-booking. Likely a different trip."
    )


# --- Local-LLM gate -------------------------------------------------

_LLM_SYSTEM = """\
You are an accounting sanity checker. Given a trip window and one expense
item, decide whether the item is plausibly part of THAT trip. Answer with
EXACTLY a JSON object: {"verdict": "YES"|"MAYBE"|"NO", "reason": "<one short sentence>"}.
No prose, no code fences. Be conservative: if the date is far outside the
trip window and the vendor doesn't justify pre-booking (airlines, hotels),
return NO."""

_LLM_USER = """\
Trip: {customer} ({trip_start} → {trip_end})
Expense: vendor={vendor!r}, date={txn_date}, amount={amount} {currency}
Description: {description!r}

Is this plausibly part of this trip? Return STRICT JSON."""


DEFAULT_LOCAL_MODEL = "llama3.2:1b"


def _have_ollama() -> bool:
    try:
        subprocess.run(["ollama", "--version"], capture_output=True, timeout=3)
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def llm_check(
    txn_date: date,
    vendor: str,
    description: str,
    amount: float,
    currency: str,
    customer: str,
    trip_start: date,
    trip_end: date,
    model: str = DEFAULT_LOCAL_MODEL,
) -> tuple[Verdict, str, str]:
    """Run the local-LLM sense check. Returns (verdict, reason, source).

    `source` is "llm:<model>" so the caller can record which model
    produced the verdict in the audit log. If Ollama isn't installed
    or the model isn't pulled, returns (MAYBE, "<reason>", "llm-unavailable").
    """
    if not _have_ollama():
        return Verdict.MAYBE, "Ollama not installed; LLM check skipped", "llm-unavailable"
    try:
        import ollama
    except ImportError:
        return Verdict.MAYBE, "ollama python lib not installed; LLM check skipped", "llm-unavailable"

    prompt = _LLM_USER.format(
        customer=customer, trip_start=trip_start.isoformat(), trip_end=trip_end.isoformat(),
        vendor=vendor, txn_date=txn_date.isoformat(), amount=amount, currency=currency,
        description=description,
    )
    try:
        resp = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            options={"temperature": 0.0, "num_predict": 96},
        )
        text = (resp.get("message") or {}).get("content", "").strip()
    except Exception as e:
        return Verdict.MAYBE, f"LLM call failed: {e}", "llm-error"

    # Pull JSON out of whatever the model emitted
    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if not m:
        return Verdict.MAYBE, f"LLM returned non-JSON: {text[:120]}", f"llm:{model}"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return Verdict.MAYBE, f"LLM JSON malformed: {text[:120]}", f"llm:{model}"

    v_raw = (data.get("verdict") or "").upper().strip()
    if v_raw not in ("YES", "MAYBE", "NO"):
        return Verdict.MAYBE, f"LLM verdict unrecognised: {v_raw!r}", f"llm:{model}"
    reason = (data.get("reason") or "")[:160] or "(no reason)"
    return Verdict(v_raw), reason, f"llm:{model}"


def check_item(
    *,
    purchase_id: str,
    txn_date: date,
    vendor: str,
    description: str,
    amount: float,
    currency: str,
    customer: str,
    trip_start: date,
    trip_end: date,
    llm_model: str = DEFAULT_LOCAL_MODEL,
    extra_airline_hints: list[str] | None = None,
    extra_hotel_hints: list[str] | None = None,
) -> Check:
    """One-shot check: deterministic first; LLM only on MAYBE.

    extra_airline_hints / extra_hotel_hints come from the overlay so the
    base skill itself carries no operator vendor list.
    """
    verdict, reason = deterministic_check(
        txn_date, vendor, description, trip_start, trip_end,
        extra_airline_hints=extra_airline_hints,
        extra_hotel_hints=extra_hotel_hints,
    )
    source = "deterministic"
    if verdict is Verdict.MAYBE:
        v2, r2, s2 = llm_check(
            txn_date, vendor, description, amount, currency,
            customer, trip_start, trip_end, model=llm_model,
        )
        verdict, reason, source = v2, r2, s2
    return Check(
        purchase_id=str(purchase_id),
        txn_date=txn_date,
        vendor=vendor,
        description=description,
        amount=amount,
        verdict=verdict,
        source=source,
        reason=reason,
    )
