"""
parse_trigger — deterministic extractor for cost-compiler trigger text.

Maps free-form initiation text (Slack DM, email body, Claude Code prompt)
into a structured TripRequest. The same shape is consumed by every
surface (CLI, Slack, email) so the runner doesn't care where the
trigger came from.

Stays deterministic per SAI principle #12 (cascade with early-stop,
never parallel): rules tier first, no LLM needed for the trigger
shape. If a field can't be inferred from the text the request stays
unresolved and the runner asks the operator on the original surface.

Public API:
    parse(text: str, overlay: dict | None = None) -> TripRequest

TripRequest fields:
    customer_hint: str | None         # "INSEAD" / "Cornell" / "ACME"
    trip_slug_hint: str | None        # "insead-2026-05"
    currency: str                      # ISO 4217; default = "USD"
    explicit_currency: bool            # True if extracted from text
    month_year: tuple[int, int] | None # (year, month) — narrows trip window search
    scope_categories: list[str]        # e.g., ["airfare", "hotels"]; [] = all
    free_text: str                     # the raw input, kept for the audit log

Per SAI #6a (schema enforcement) the output is a Pydantic-shaped dict
with `extra="forbid"`. Unknown fields raise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


# Currency aliases used in chat-style initiation.
_CURRENCY_ALIASES = {
    "USD": ["usd", "us dollar", "us dollars", "dollar", "dollars", "$"],
    "EUR": ["eur", "euro", "euros", "€"],
    "GBP": ["gbp", "pound", "pounds", "£"],
    "CHF": ["chf", "swiss franc", "swiss francs"],
    "CAD": ["cad", "canadian dollar"],
    "JPY": ["jpy", "yen", "¥"],
}

# Trip-scope keywords. Each maps to a canonical category name. The
# overlay's expense_accounts map uses the same canonical strings.
_SCOPE_KEYWORDS = {
    "airfare": ["airfare", "flight", "flights", "ticket", "plane", "air ticket"],
    "hotels": ["hotel", "hotels", "lodging", "accommodation", "stay"],
    "taxis_rideshare": ["taxi", "taxis", "uber", "lyft", "rideshare", "cab"],
    "travel_meals": ["meal", "meals", "food", "dinner", "lunch", "breakfast"],
}

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True)
class TripRequest:
    customer_hint: str | None = None
    trip_slug_hint: str | None = None
    currency: str = "USD"
    explicit_currency: bool = False
    month_year: tuple[int, int] | None = None
    explicit_date_range: tuple[str, str] | None = None  # (start_iso, end_iso)
    scope_categories: tuple[str, ...] = field(default_factory=tuple)
    free_text: str = ""

    def to_dict(self) -> dict:
        return {
            "customer_hint": self.customer_hint,
            "trip_slug_hint": self.trip_slug_hint,
            "currency": self.currency,
            "explicit_currency": self.explicit_currency,
            "month_year": list(self.month_year) if self.month_year else None,
            "explicit_date_range": list(self.explicit_date_range) if self.explicit_date_range else None,
            "scope_categories": list(self.scope_categories),
            "free_text": self.free_text,
        }


def _detect_currency(lower: str) -> tuple[str, bool]:
    """Return (currency, explicit). Defaults to USD when nothing matches."""
    # Prefer most-specific tokens first ("us dollars" before "dollar").
    for ccy, aliases in _CURRENCY_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            # Match a whole token where alias is letters; otherwise match symbol.
            if alias.isalpha():
                pattern = r"\b" + re.escape(alias) + r"\b"
            else:
                pattern = re.escape(alias)
            if re.search(pattern, lower, re.IGNORECASE):
                return ccy, True
    return "USD", False


def _detect_customer(text: str, known_customer_names: Iterable[str] | None) -> str | None:
    """First overlay-known name found in text wins; else None."""
    if not known_customer_names:
        return None
    upper = text.upper()
    for name in known_customer_names:
        if not name:
            continue
        if re.search(r"\b" + re.escape(name.upper()) + r"\b", upper):
            return name
    return None


def _detect_trip_slug(text: str) -> str | None:
    """Pattern: <customer>-<YYYY>-<MM> (e.g., insead-2026-05).

    Also accepts uppercase variants. Returns the canonical lowercase
    form, no further normalisation.
    """
    m = re.search(r"\b([a-z]{2,20})-(\d{4})-(\d{2})\b", text, re.IGNORECASE)
    if m:
        return f"{m.group(1).lower()}-{m.group(2)}-{m.group(3)}"
    return None


def _detect_month_year(text: str) -> tuple[int, int] | None:
    """Look for 'May 2026' / 'in May' / '2026-05' / 'May 5–18, 2026' patterns.

    Returns (year, month). When only the month is named, assume the
    current year (or the operator follows up with --start/--end on the
    CLI; this is a hint, not a requirement).
    """
    # ISO-ish "2026-05"
    m = re.search(r"\b(\d{4})-(\d{2})\b", text)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 2000 <= y <= 2100:
            return (y, mo)
    # Tolerant "<MonthName> ... <YYYY>" — allows day ranges in between
    # (e.g., "May 5–18, 2026" / "May 5-18, 2026" / "May 5 to 18, 2026").
    # We require the year to appear within ~30 chars of the month name
    # to keep precision; a sentence-distant year shouldn't bind.
    for name, mo in _MONTH_NAMES.items():
        m = re.search(
            rf"\b{name}\b[^A-Za-z]{{0,30}}?(\d{{4}})\b",
            text, re.IGNORECASE,
        )
        if m:
            y = int(m.group(1))
            if 2000 <= y <= 2100:
                return (y, mo)
    return None


def _detect_explicit_date_range(text: str) -> tuple[str, str] | None:
    """Look for an explicit date range like 'May 5–18, 2026' or '2026-05-05 to 2026-05-18'.

    Returns (start_iso, end_iso). This gives the dispatcher a precise
    window rather than the generous month-window fallback used when
    only month_year is known.
    """
    # ISO range: "2026-05-05 to 2026-05-18" / "2026-05-05..2026-05-18"
    m = re.search(
        r"\b(\d{4}-\d{2}-\d{2})\s*(?:to|—|–|-|\.\.)\s*(\d{4}-\d{2}-\d{2})\b",
        text,
    )
    if m:
        return (m.group(1), m.group(2))
    # "<MonthName> <D>–<D>, <YYYY>" (en-dash, em-dash, or hyphen)
    for name, mo in _MONTH_NAMES.items():
        m = re.search(
            rf"\b{name}\s+(\d{{1,2}})\s*[–—\-]\s*(\d{{1,2}})\s*,?\s*(\d{{4}})\b",
            text, re.IGNORECASE,
        )
        if m:
            d1 = int(m.group(1))
            d2 = int(m.group(2))
            y = int(m.group(3))
            if 1 <= d1 <= 31 and 1 <= d2 <= 31 and d1 <= d2 and 2000 <= y <= 2100:
                return (
                    f"{y:04d}-{mo:02d}-{d1:02d}",
                    f"{y:04d}-{mo:02d}-{d2:02d}",
                )
    # "<MonthName> <D> to <MonthName> <D>, <YYYY>" (cross-month)
    for n1, mo1 in _MONTH_NAMES.items():
        for n2, mo2 in _MONTH_NAMES.items():
            m = re.search(
                rf"\b{n1}\s+(\d{{1,2}})\s+to\s+{n2}\s+(\d{{1,2}})\s*,?\s*(\d{{4}})\b",
                text, re.IGNORECASE,
            )
            if m:
                d1 = int(m.group(1))
                d2 = int(m.group(2))
                y = int(m.group(3))
                if 1 <= d1 <= 31 and 1 <= d2 <= 31 and 2000 <= y <= 2100:
                    return (
                        f"{y:04d}-{mo1:02d}-{d1:02d}",
                        f"{y:04d}-{mo2:02d}-{d2:02d}",
                    )
    return None


def _detect_scope(lower: str) -> tuple[str, ...]:
    """Return canonical category names referenced in the text.

    Empty tuple means "no explicit scope" — the caller should treat
    that as "all in-window costs."
    """
    found: list[str] = []
    for canonical, words in _SCOPE_KEYWORDS.items():
        if any(re.search(r"\b" + re.escape(w) + r"\b", lower) for w in words):
            found.append(canonical)
    # Dedup while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for c in found:
        if c not in seen:
            ordered.append(c)
            seen.add(c)
    return tuple(ordered)


def derive_plan(req: TripRequest) -> list[dict]:
    """Map a TripRequest into a deterministic ordered subcommand plan.

    Each entry is {step: <atomic step name>, args: {...}}. The dispatcher
    walks the list and calls the matching runner subcommand. Stays in
    rules tier (no LLM) so the trigger surface is auditable.

    The plan covers steps 0-10 except those operator-bound:
      - step 2 REVIEW + approval gate is included as `await-approval`
      - step 5 INVOICE is gated behind approval; the dispatcher
        consults the approval state before running create-invoice
      - steps 1c (Photos) and 1d (Calendar pre-booking) run only when
        the trigger explicitly asks for them

    `req` MUST carry a trip_slug_hint OR (month_year + customer_hint)
    or the plan is incomplete; the dispatcher refuses (fail-closed).
    """
    # Resolve a slug.
    slug = req.trip_slug_hint
    if not slug and req.customer_hint and req.month_year:
        y, m = req.month_year
        slug = f"{req.customer_hint.lower()}-{y:04d}-{m:02d}"
    if not slug:
        # Incomplete; dispatcher will refuse.
        return []

    # Resolve a date window.
    # Priority order:
    #   1. Explicit date range parsed from the trigger ("May 5–18, 2026")
    #   2. Month + year ("May 2026" → full month)
    #   3. None — dispatcher refuses with a friendly clarification
    start_iso = end_iso = None
    if req.explicit_date_range:
        start_iso, end_iso = req.explicit_date_range
    elif req.month_year:
        import calendar
        y, m = req.month_year
        start_iso = f"{y:04d}-{m:02d}-01"
        last_day = calendar.monthrange(y, m)[1]
        end_iso = f"{y:04d}-{m:02d}-{last_day:02d}"

    plan: list[dict] = []
    plan.append({"step": "parse-trigger",
                 "args": {"text": req.free_text}})
    if start_iso and end_iso:
        plan.append({"step": "scan-cards",
                     "args": {"start": start_iso, "end": end_iso}})
        plan.append({"step": "search-receipts",
                     "args": {"start": start_iso, "end": end_iso}})
        plan.append({"step": "attach-onsite-photos",
                     "args": {"start": start_iso, "end": end_iso, "trip": slug}})
        plan.append({"step": "extract-pre-bookings",
                     "args": {"start": start_iso, "end": end_iso,
                              "customer": req.customer_hint}})
    plan.append({"step": "await-approval",
                 "args": {"trip": slug,
                          "prompt": f"Approve the cost-compiler run for "
                                    f"{req.customer_hint or '<customer>'} "
                                    f"trip {slug}?"}})
    plan.append({"step": "create-invoice",
                 "args": {"trip": slug,
                          "currency": req.currency,
                          "plan": f"<overlay>/trip_runs/{slug}/plan.json"}})
    plan.append({"step": "tag-purchases",
                 "args": {"trip": slug,
                          "customer": req.customer_hint or ""}})
    plan.append({"step": "match-receipts-to-purchases",
                 "args": {"trip": slug,
                          "customer": req.customer_hint or "",
                          "start": start_iso, "end": end_iso}})
    if start_iso and end_iso:
        plan.append({"step": "sense-check",
                     "args": {"trip": slug,
                              "customer": req.customer_hint or "",
                              "start": start_iso, "end": end_iso}})
        plan.append({"step": "reconcile-billables",
                     "args": {"trip": slug,
                              "plan": f"<overlay>/trip_runs/{slug}/plan.json",
                              "start": start_iso, "end": end_iso}})
    return plan


def parse(text: str, overlay: dict | None = None) -> TripRequest:
    """Parse free-form initiation text into a TripRequest.

    overlay is the loaded identity.yaml (may be None for unit tests).
    The known customer names come from `default_customer.hint`
    + any future `customers:` list the overlay decides to ship.

    No LLM is invoked; this is deterministic rules tier per #12.
    """
    text = (text or "").strip()
    lower = text.lower()

    customer_names: list[str] = []
    if overlay:
        dc = (overlay.get("default_customer") or {}).get("hint")
        if dc:
            customer_names.append(dc)
        # Future-ready: overlay may add `customers: ["INSEAD", "Cornell", ...]`
        more = overlay.get("customers") or []
        for c in more:
            if c and c not in customer_names:
                customer_names.append(c)

    currency, explicit_currency = _detect_currency(lower)
    customer_hint = _detect_customer(text, customer_names)

    # If the operator didn't explicitly state a currency but the
    # matched customer has a known billing currency in the overlay,
    # use that. The spec says "If he does not say anything assume
    # DOLLAR" — but a customer-specific currency is a stronger signal
    # than the global default. `explicit_currency` stays False so
    # `cmd_create_invoice`'s precedence chain still treats this as a
    # fallback (the QB customer's currency wins anyway downstream).
    if (not explicit_currency
            and customer_hint
            and overlay
            and (overlay.get("default_customer") or {}).get("hint") == customer_hint):
        cust_ccy = (overlay.get("default_customer") or {}).get("currency")
        if cust_ccy:
            currency = cust_ccy.upper()

    return TripRequest(
        customer_hint=customer_hint,
        trip_slug_hint=_detect_trip_slug(text),
        currency=currency,
        explicit_currency=explicit_currency,
        month_year=_detect_month_year(text),
        explicit_date_range=_detect_explicit_date_range(text),
        scope_categories=_detect_scope(lower),
        free_text=text,
    )
