"""
Gmail receipt search helpers (atomic, base skill).

Generic searches by vendor sender, date window, and keyword. No hardcoded
vendor list — the overlay supplies the list via config.

Public API (atomic):
    build_receipt_query(senders, start_date, end_date, keywords=None)
    extract_amounts(text)
    canonical_vendor(sender) - rough mapping of sender domain to vendor name

Functions return data structures only. They DO NOT call Gmail directly —
the runner / overlay glues these to whichever Gmail tool is available
(MCP, gmail-api Python library, etc.). Keeps the base skill testable
without network.
"""
from __future__ import annotations

import re
from datetime import date


def build_receipt_query(
    senders: list[str],
    start_date: date,
    end_date: date,
    keywords: list[str] | None = None,
) -> str:
    """Compose a Gmail search query.

    senders: list of email addresses or domain patterns (e.g. ["@uber.com", "@lyft.com"]).
    keywords: optional positive keywords to AND in (e.g. ["receipt", "trip"]).
    """
    parts = []
    if senders:
        from_parts = " OR ".join(f"from:{s}" for s in senders)
        parts.append(f"({from_parts})")
    if keywords:
        kw_parts = " OR ".join(f'"{k}"' for k in keywords)
        parts.append(f"({kw_parts})")
    parts.append(f"after:{start_date.strftime('%Y/%m/%d')}")
    parts.append(f"before:{end_date.strftime('%Y/%m/%d')}")
    return " ".join(parts)


_AMOUNT_RE = re.compile(r"(?:USD|EUR|GBP|CHF|\$|€|£|CHF)\s?([\d,]+\.\d{2})|([\d,]+\.\d{2})\s?(?:USD|EUR|GBP|CHF)")
_TOTAL_HINT_RE = re.compile(
    r"(total|charged|grand\s*total|amount\s*due|trip\s*total)[^a-z0-9]{0,40}([\$€£]?\s?[\d,]+\.\d{2})",
    re.I,
)


def extract_amounts(text: str) -> dict[str, list[float]]:
    """Pull monetary amounts out of a receipt email body.

    Returns {"all": [...], "totals": [...]} where `totals` are amounts adjacent
    to a "total / charged" hint word and most likely the receipt grand total.
    """
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain)

    all_amounts = []
    for m in _AMOUNT_RE.finditer(plain):
        val = (m.group(1) or m.group(2) or "").replace(",", "")
        try:
            all_amounts.append(float(val))
        except ValueError:
            pass

    totals = []
    for m in _TOTAL_HINT_RE.finditer(plain):
        raw = re.sub(r"[^\d.]", "", m.group(2))
        try:
            totals.append(float(raw))
        except ValueError:
            pass

    return {"all": sorted(set(all_amounts), reverse=True), "totals": sorted(set(totals), reverse=True)}


def canonical_vendor(sender: str, sender_to_vendor: dict | None = None) -> str:
    """Map a Gmail sender address to a canonical vendor name.

    The base skill knows nothing about any operator's vendor roster. Pass
    `sender_to_vendor` from the overlay (`overlay['gmail_sender_to_vendor']`)
    to do specific lookups. The fallback is to use the sender's apex
    domain capitalized — which gives a usable but generic name.

    Example overlay shape:
        gmail_sender_to_vendor:
          uber.com:       "Uber"
          lyftmail.com:   "Lyft"
          united.com:     "United Airlines"
    """
    s = sender.lower()
    if sender_to_vendor:
        for needle, vendor_name in sender_to_vendor.items():
            if needle.lower() in s:
                return vendor_name
    # Domain-based fallback: keep the email domain so the caller still
    # gets a non-empty label without any operator config.
    if "@" in s:
        domain = s.split("@", 1)[1].split(".")[0]
        return domain.capitalize()
    return s
