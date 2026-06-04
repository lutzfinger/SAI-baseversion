"""Pure, deterministic logic for trip-mileage-log (no I/O, fully unit-testable).

Every function here is a pure transform of in-memory data, so canaries and the
cascade tiers share ONE source of truth. The connectors (Calendar, Google
Sheet) and the cascade live elsewhere; this module never touches the network.

Sheet column convention (tab "2026 Time Tracking"):
  A=Day B=morning C=Airport D=Airport_to E=Booking F=evening
  G=Travel Day  H=Other Travel in Miles  I=% of Business  J=Reason
Home is Mountain View; H is ROUND-TRIP ("back & forth") miles; one-way ≈ H/2.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Iterable

HOME_DEFAULT = "Mountain View"

# Bay-Area gazetteer — extend per-operator via config place_aliases.
_KNOWN_CITIES = (
    "Mountain View", "Palo Alto", "Berkeley", "San Francisco", "San Jose",
    "Oakland", "Menlo Park", "Sunnyvale", "Santa Clara", "Cupertino",
    "Redwood City", "Stanford", "Los Altos", "Saratoga", "Campbell",
    "Fremont", "Emeryville", "South San Francisco", "San Mateo", "Burlingame",
    "Foster City", "Milpitas", "Hayward", "Walnut Creek", "San Rafael",
    "Sausalito", "Daly City", "Belmont", "San Carlos", "Half Moon Bay",
)
_VIRTUAL_MARKERS = (
    "zoom", "meet.google", "google meet", "microsoft teams", "teams meeting",
    "teams-besprechung", "webex", "virtual", "http", "phone", "conference id",
    "dial-in", "dial in",
)
_FLIGHT_WORDS = re.compile(r"\b(flight|airport|airline|boarding|terminal|red[- ]?eye|layover)\b", re.I)
_FLIGHT_NUM = re.compile(r"\b[A-Z]{2}\d{2,4}\b")  # e.g. DL2123, UA88 (case-sensitive)

_COL_INDEX = {c: i for i, c in enumerate("ABCDEFGHIJ")}


# ─── column access (row may be a dict {"A":..} or a list [A,B,...]) ──────

def col(row: Any, letter: str) -> str:
    if isinstance(row, dict):
        return str(row.get(letter, "") or "")
    if isinstance(row, (list, tuple)):
        idx = _COL_INDEX[letter]
        return str(row[idx]) if idx < len(row) and row[idx] is not None else ""
    return ""


def _canon(s: str) -> str:
    return " ".join(str(s).strip().lower().split())


# ─── 1. date parsing (tense-aware) ──────────────────────────────────────

_MONTHS = {
    m.lower(): i for i, m in enumerate(
        ("January February March April May June July August September "
         "October November December").split(), start=1)
}


def parse_trip_date(utterance: str, today: date) -> tuple[str | None, bool]:
    """Return (YYYY-MM-DD | None, prospective). 'now/I am going/tomorrow' are
    prospective; 'yesterday/went' are past. Unparseable -> (None, False)."""
    u = (utterance or "").lower()
    going = bool(re.search(r"\bi am going\b|\bi'm going\b|\bgoing to\b|\bwill go\b", u))
    past_verb = "went" in u or "drove" in u

    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", u)
    if m:
        try:
            dt = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None, False
        return dt.isoformat(), dt > today
    if "yesterday" in u:
        return (today - timedelta(days=1)).isoformat(), False
    if "tomorrow" in u:
        return (today + timedelta(days=1)).isoformat(), True
    if "today" in u or "now" in u or going:
        return today.isoformat(), (going and not past_verb)
    m = re.search(r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", u)
    if m and m.group(1) in _MONTHS:
        try:
            dt = date(today.year, _MONTHS[m.group(1)], int(m.group(2)))
        except ValueError:
            return None, False
        return dt.isoformat(), dt > today
    return None, False


# ─── 2. place normalization + destination resolution ────────────────────

def normalize_place(raw: str, aliases: dict[str, str] | None = None) -> str | None:
    """Canonical place from a messy address / summary / description, or None
    for a virtual/locationless value."""
    if not raw:
        return None
    s = str(raw).strip()
    low = s.lower()
    if any(v in low for v in _VIRTUAL_MARKERS):
        return None
    for sub, canon in (aliases or {}).items():
        if str(sub).lower() in low:
            return canon
    for city in _KNOWN_CITIES:
        if re.search(r"\b" + re.escape(city.lower()) + r"\b", low):
            return city
    m = re.search(r"\b(?:in|@|at)\s+([A-Z][A-Za-z .'\-]+)", s)
    if m:
        cand = m.group(1).strip(" .-")
        # stop at a comma/segment boundary
        cand = re.split(r"[,/]", cand)[0].strip()
        return cand or None
    return None


def resolve_destinations(
    events: Iterable[dict],
    home_label: str = HOME_DEFAULT,
    aliases: dict[str, str] | None = None,
    utterance_hint: str | None = None,
) -> dict:
    """Ordered distinct non-home places from the day's events.

    Returns {"places":[...], "events_used":[...], "low_confidence":bool,
             "too_many":bool}.
    """
    home = home_label or HOME_DEFAULT
    evs = sorted(list(events or []), key=lambda e: str(e.get("start", "") or ""))
    places: list[str] = []
    used: list[str] = []
    seen: set[str] = set()
    for ev in evs:
        place = None
        for field in ("location", "summary", "description"):
            place = normalize_place(ev.get(field, ""), aliases)
            if place:
                break
        if not place or _canon(place) == _canon(home):
            continue
        # Record EVERY event at a kept destination (deduped), so the reason note
        # lists all of them (e.g. both "One Medical" AND "esade lutz" for the
        # Berkeley stop) rather than only the first event per place.
        label = (ev.get("summary") or ev.get("location") or "").strip()
        if label and label not in used:
            used.append(label)
        if _canon(place) in seen:
            continue
        seen.add(_canon(place))
        places.append(place)
    low_conf = False
    if not places and utterance_hint:
        h = normalize_place(utterance_hint, aliases) or str(utterance_hint).strip().title()
        if h and _canon(h) != _canon(home):
            places = [h]
            used = [f"(from your message: '{utterance_hint}')"]
            low_conf = True
    return {
        "places": places,
        "events_used": used,
        "low_confidence": low_conf,
        "too_many": len(places) > 2,
    }


# ─── 3. flight + relocation gates (NOT keyed on column G — see plan) ─────

def is_flight_day(row: Any, events: Iterable[dict]) -> tuple[bool, str]:
    c, d = col(row, "C").strip(), col(row, "D").strip()
    if c or d:
        return True, f"airport_in_sheet(C={c!r},D={d!r})"
    for ev in (events or []):
        summary = str(ev.get("summary", "") or "")
        text = f"{summary} {ev.get('location','')}"
        if _FLIGHT_WORDS.search(text) or _FLIGHT_NUM.search(summary):
            return True, f"flight_event({summary[:40]!r})"
    return False, "no airport booking and no flight event"


def is_relocation(row: Any) -> tuple[bool, str]:
    b, f = col(row, "B").strip(), col(row, "F").strip()
    if not (b and f):
        return False, ""
    # Fall back to the raw label when a place isn't in the gazetteer (e.g.
    # "Ithaca") so an end-of-day relocation still trips the gate.
    nb = normalize_place(b) or b
    nf = normalize_place(f) or f
    if _canon(nb) != _canon(nf):
        return True, f"morning={b!r}_evening={f!r}"
    return False, ""


# ─── 4. date-row match + overwrite guard ────────────────────────────────

def find_date_row(col_a_values: list[str], date_str: str) -> int | None:
    for i, v in enumerate(col_a_values, start=1):
        if str(v or "").strip() == date_str:
            return i
    return None


def row_conflict(row: Any) -> list[str]:
    return [c for c in ("H", "I", "J") if col(row, c).strip()]


# ─── 5. distance lookup + chained-loop miles ────────────────────────────

def parse_distance_tab(rows: Iterable[list]) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
    """Parse the 'Distance MTV to' tab. Place rows -> round-trip miles;
    'A -> B' rows -> inter-stop leg miles."""
    rt: dict[str, float] = {}
    leg: dict[tuple[str, str], float] = {}
    for r in (rows or []):
        if not r or not str(r[0] or "").strip():
            continue
        name = str(r[0]).strip()
        val = str(r[1]).strip() if len(r) > 1 and r[1] is not None else ""
        if not val:
            continue
        try:
            miles = float(val.replace(",", "").split()[0])
        except (ValueError, IndexError):
            continue
        if "->" in name or "→" in name:
            a, b = re.split(r"->|→", name, maxsplit=1)
            leg[(_canon(a), _canon(b))] = miles
        else:
            rt[_canon(name)] = miles
    return rt, leg


def _leg_lookup(leg: dict, a: str, b: str) -> float | None:
    return leg.get((_canon(a), _canon(b))) or leg.get((_canon(b), _canon(a)))


def chained_loop_miles(
    places: list[str],
    rt: dict[str, float],
    leg: dict[tuple[str, str], float],
    provided: dict | None = None,
) -> dict:
    """Compute miles for the day. single -> round trip; two -> chained loop
    home->A->B->home = rt(A)/2 + leg(A,B) + rt(B)/2 (one-way ≈ round-trip/2).
    On any miss return {"ask": <actionable>, "missing": {...}}."""
    provided = provided or {}
    prt = {_canon(k): v for k, v in (provided.get("round_trip") or {}).items()}
    pleg = {(_canon(a), _canon(b)): v for (a, b), v in (provided.get("leg") or {}).items()} \
        if provided.get("leg") else {}

    def rt_of(p):
        return prt.get(_canon(p), rt.get(_canon(p)))

    if not places:
        return {"ask": "No destination found — tell me where you drove.", "missing": {"places": []}}

    if len(places) == 1:
        p = places[0]
        m = rt_of(p)
        if m is None:
            return {"ask": f"I don't have the round-trip miles for {p}. "
                           f"Reply with the round-trip ('back & forth') miles from {HOME_DEFAULT} to {p}.",
                    "missing": {"round_trip": [p]}}
        new = [] if _canon(p) in rt else [{"name": p, "miles": m}]
        return {"miles": round(float(m), 2),
                "breakdown": f"{p} round trip = {m:g} mi",
                "new_entries": new}

    if len(places) == 2:
        a, b = places
        ra, rb = rt_of(a), rt_of(b)
        lg = pleg.get((_canon(a), _canon(b))) or pleg.get((_canon(b), _canon(a))) or _leg_lookup(leg, a, b)
        missing = {"round_trip": [], "leg": []}
        if ra is None:
            missing["round_trip"].append(a)
        if rb is None:
            missing["round_trip"].append(b)
        if lg is None:
            missing["leg"].append(f"{a} -> {b}")
        if missing["round_trip"] or missing["leg"]:
            return {"ask": _ask_two(missing, a, b), "missing": missing}
        miles = ra / 2.0 + lg + rb / 2.0
        new = []
        if _canon(a) not in rt and ra is not None:
            new.append({"name": a, "miles": ra})
        if _canon(b) not in rt and rb is not None:
            new.append({"name": b, "miles": rb})
        if _leg_lookup(leg, a, b) is None and lg is not None:
            new.append({"name": f"{a} -> {b}", "miles": lg})
        return {"miles": round(miles, 2),
                "breakdown": (f"{a} one-way {ra/2:g} + {a}->{b} leg {lg:g} + {b} one-way {rb/2:g} "
                              f"(one-way ≈ round-trip/2)"),
                "new_entries": new}

    return {"ask": "More than two destinations that day — please log this one manually.",
            "missing": {"too_many": places}}


def _ask_two(missing: dict, a: str, b: str) -> str:
    bits = []
    if missing["round_trip"]:
        bits.append("round-trip miles from %s for: %s" % (HOME_DEFAULT, ", ".join(missing["round_trip"])))
    if missing["leg"]:
        bits.append("the driving leg %s" % ", ".join(missing["leg"]))
    return ("Two-stop day (%s then %s). I need %s. Reply with the number(s) and I'll log it."
            % (a, b, " and ".join(bits)))


# ─── 6. reason + draft assembly ─────────────────────────────────────────

def build_reason(
    date_str: str,
    places: list[str],
    events_used: list[str],
    miles_breakdown: str,
    not_flying_evidence: str,
    *,
    business_pct: int = 100,
    prospective: bool = False,
    low_confidence: bool = False,
) -> str:
    parts: list[str] = []
    if prospective:
        parts.append("PROSPECTIVE — confirm you actually drove this.")
    route = " → ".join([HOME_DEFAULT] + places + [HOME_DEFAULT]) if places else HOME_DEFAULT
    parts.append(f"{date_str}: drove {route}.")
    real = [e for e in (events_used or []) if e and not e.startswith("(from your message")]
    if real:
        parts.append("Calendar: " + "; ".join(real) + ".")
    parts.append(f"Not a flight ({not_flying_evidence}).")
    parts.append(f"Business {business_pct}%.")
    parts.append(f"Miles: {miles_breakdown}.")
    if low_confidence:
        parts.append("(Destination inferred from your message, not a calendar location — please verify.)")
    return " ".join(parts)


def build_row_draft(
    row: int,
    miles: float,
    reason: str,
    *,
    business_pct: int = 100,
    new_distance_entries: list[dict] | None = None,
    prospective: bool = False,
    overwrote: bool = False,
) -> dict:
    """The dict the human tier stages under accumulated['draft'] and that
    send_tool.py consumes."""
    return {
        "workflow_id": "trip-mileage-log",
        "row": int(row),
        "H": float(miles),
        "I": int(business_pct),
        "J": reason,
        "new_distance_entries": new_distance_entries or [],
        "prospective": bool(prospective),
        "overwrote": bool(overwrote),
    }
