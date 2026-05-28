"""
Trip-window inference from a Google Calendar.

Given a search hint (a free-text fragment supplied by the operator at
trigger time) and a search window, find the multi-day block of travel
events. Returns a proposed window plus the supporting events.

Vendor-name and airport-code lists used as travel-keyword hints come
from the overlay (`overlay['calendar']['travel_keywords']`) so the base
skill carries no operator-specific identifiers. A generic fallback list
is supplied for first-run setup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


# Generic, vendor-agnostic fallback. Operator overlays override this
# with their own travel_keywords entry pointing at the airlines/hotels
# they actually use.
_GENERIC_FALLBACK = (
    r"flight|hotel|airline|airport|taxi|rideshare|"
    r"travel|trip|out of office|OOO"
)


def build_travel_keywords_re(extra_keywords: list[str] | None = None) -> re.Pattern:
    """Compile the travel-keyword regex.

    extra_keywords: an operator-supplied list of vendor names and airport
    codes from `overlay['calendar']['travel_keywords']`. They're OR'd with
    the generic fallback so calendar inference works on day-1 even before
    the operator customises the list.
    """
    pat = _GENERIC_FALLBACK
    if extra_keywords:
        joined = "|".join(re.escape(k) for k in extra_keywords if k)
        if joined:
            pat = pat + "|" + joined
    return re.compile(pat, re.IGNORECASE)


# Default compiled regex for callers that don't pass overlay keywords yet.
# This keeps backward compatibility — existing call sites continue to work.
TRAVEL_KEYWORDS = build_travel_keywords_re()


@dataclass
class TripWindow:
    start: date
    end: date
    supporting_events: list[dict]
    multi_day_block_event_id: Optional[str] = None


def event_start(ev: dict) -> Optional[date]:
    s = ev.get("start", {})
    raw = s.get("dateTime") or s.get("date")
    if not raw:
        return None
    return _parse_date(raw)


def event_end(ev: dict) -> Optional[date]:
    e = ev.get("end", {})
    raw = e.get("dateTime") or e.get("date")
    if not raw:
        return None
    return _parse_date(raw)


def _parse_date(s: str) -> date:
    # accept either "2026-05-06" or "2026-05-06T13:15:00-07:00"
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return datetime.fromisoformat(s).date()


def find_trip_window(events: list[dict], hint: str = "") -> Optional[TripWindow]:
    """Pick the trip window from a list of calendar events.

    Strategy:
        1. Identify the LONGEST multi-day all-day event matching `hint` or
           travel keywords. That's the trip core.
        2. Extend the window to include any travel-keyword events ±1 day
           around it.
    """
    hint_re = re.compile(re.escape(hint), re.IGNORECASE) if hint else None

    candidates = []
    for ev in events:
        s = event_start(ev)
        e = event_end(ev)
        if not s or not e:
            continue
        summary = ev.get("summary") or ""
        is_all_day = ev.get("start", {}).get("date") is not None
        duration_days = (e - s).days
        if is_all_day and duration_days >= 1:
            keyword_match = (hint_re.search(summary) if hint_re else False) or TRAVEL_KEYWORDS.search(summary)
            if keyword_match:
                candidates.append((duration_days, ev, s, e))

    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    longest_dur, longest_ev, core_start, core_end = candidates[0]

    # widen window by sweeping for travel events within +/- 2 days
    window_start = core_start
    window_end = core_end
    supporting = [longest_ev]
    for ev in events:
        s = event_start(ev)
        if not s:
            continue
        summary = ev.get("summary") or ""
        loc = ev.get("location") or ""
        is_travel = bool(TRAVEL_KEYWORDS.search(summary) or TRAVEL_KEYWORDS.search(loc))
        if not is_travel:
            continue
        e = event_end(ev) or s
        # if this travel event is within +/- 2 days of the core, extend window
        if s >= core_start.replace(day=max(1, core_start.day - 2)) and s <= core_end:
            supporting.append(ev)
            if s < window_start:
                window_start = s
            if e > window_end:
                window_end = e

    return TripWindow(
        start=window_start,
        end=window_end,
        supporting_events=supporting,
        multi_day_block_event_id=longest_ev.get("id"),
    )


# ----------------------------------------------------------------------
# Pre-booking extraction (Phase B.3)
# ----------------------------------------------------------------------

# Generic vendor-category patterns. Operator-specific brand names come
# from the overlay (`overlay['sense_check']['airline_hints']` etc.).
# "booking" / "reservation" are generic terms that apply to either —
# they're NOT in these category-specific patterns; the kind regex
# needs a category-specific signal to fire.
_GENERIC_AIRLINE_RE = r"airline|airways|airlines|flight|carrier|airfare|e-?ticket\b|boarding"
_GENERIC_HOTEL_RE = r"hotel|inn|resort|hostel|airbnb|lodging|residence|guest house"


@dataclass
class PreBooking:
    event_id: str
    event_summary: str
    event_start: date
    days_before_trip: int
    kind: str           # "flight" | "hotel" | "unknown"
    confidence: str     # "high" | "medium" | "low"
    location: str = ""
    description: str = ""


def _kind_regex(generic: str, extra: list[str] | None) -> re.Pattern:
    pat = generic
    if extra:
        joined = "|".join(re.escape(w) for w in extra if w)
        if joined:
            pat = pat + "|" + joined
    return re.compile(pat, re.IGNORECASE)


def extract_pre_bookings(
    events: list[dict],
    trip_start: date,
    trip_end: date,
    *,
    pre_window_days: int = 180,
    min_days_before: int = 7,
    extra_airline_hints: list[str] | None = None,
    extra_hotel_hints: list[str] | None = None,
    destination_hints: list[str] | None = None,
) -> list[PreBooking]:
    """Find calendar events likely to represent pre-booked travel for this trip.

    Algorithm:
      1. Look at events starting between (trip_start - pre_window_days)
         and (trip_start - min_days_before).
      2. Score each event:
         - flight regex hit → kind=flight, confidence=high
         - hotel regex hit  → kind=hotel,  confidence=high
         - both hit         → kind=flight (likely the dominant signal)
         - travel keyword hit but no kind → kind=unknown, confidence=low
      3. If `destination_hints` is supplied, downgrade events whose
         summary/location contain NONE of the hints — they're probably
         a different trip's pre-booking.

    Returns list sorted by (-days_before_trip, summary). Empty list if
    nothing qualifies.

    Operator-specific brand names come from the overlay; this function
    accepts them via kwargs so the base skill itself carries no operator
    vendor list.
    """
    airline_re = _kind_regex(_GENERIC_AIRLINE_RE, extra_airline_hints)
    hotel_re = _kind_regex(_GENERIC_HOTEL_RE, extra_hotel_hints)
    dest_re = None
    if destination_hints:
        words = "|".join(re.escape(d) for d in destination_hints if d)
        if words:
            dest_re = re.compile(words, re.IGNORECASE)

    earliest_pre = date.fromordinal(trip_start.toordinal() - pre_window_days)
    latest_pre = date.fromordinal(trip_start.toordinal() - min_days_before)

    out: list[PreBooking] = []
    for ev in events:
        s = event_start(ev)
        if not s:
            continue
        if not (earliest_pre <= s <= latest_pre):
            continue
        summary = ev.get("summary") or ""
        location = ev.get("location") or ""
        description = ev.get("description") or ""
        text = " ".join([summary, location, description])
        airline_hit = bool(airline_re.search(text))
        hotel_hit = bool(hotel_re.search(text))
        if not (airline_hit or hotel_hit):
            # Not a travel/booking event at all.
            continue
        if dest_re and not dest_re.search(text):
            # Looks like travel, but for a different destination.
            confidence = "low"
        else:
            confidence = "high"
        # Tie-break: if both fire, prefer hotel (more specific signal —
        # "flight" appears generically; "hotel"/"inn" are usually only
        # used in lodging context).
        if hotel_hit:
            kind = "hotel"
        elif airline_hit:
            kind = "flight"
        else:
            kind = "unknown"
        out.append(PreBooking(
            event_id=ev.get("id", ""),
            event_summary=summary,
            event_start=s,
            days_before_trip=(trip_start - s).days,
            kind=kind,
            confidence=confidence,
            location=location,
            description=description[:200],
        ))
    out.sort(key=lambda p: (-p.days_before_trip, p.event_summary))
    return out
