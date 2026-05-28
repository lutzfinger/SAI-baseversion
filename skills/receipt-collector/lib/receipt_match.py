"""
Targeted Gmail receipt lookup, per QB Purchase.

`download-receipts` does a broad date-window sweep — useful for discovery,
but it pulls in unrelated receipts whenever booking lead-time falls outside
the trip window (a flight booked 3 months early has its eTicket outside
the trip dates).

`match-receipts-to-purchases` instead reads the trip-marked Purchases from
QB and, for each one, derives a precise Gmail query from the Purchase's
own memo + vendor. Output: one folder per Purchase, plus an index.md
that maps each downloaded thread back to the Purchase it covers.

Search-derivation rules live in :func:`derive_search` and are pure
heuristics over text. When a Purchase has no Gmail equivalent (paid on
site — taxi cash, hotel front desk), the derivation returns ``None`` and
the runner notes "no email receipt expected" in index.md so the operator
knows to attach a photo manually.
"""
from __future__ import annotations

import re
from pathlib import Path


# Generic confirmation-number shape: 5–8 alnum, ALL CAPS, often in the line description
_CONF_RE = re.compile(r"\b([A-Z0-9]{5,8})\b")

# Generic noise tokens we never treat as confirmation numbers
_CONF_STOPWORDS = frozenset({
    "USD", "EUR", "GBP", "CHF", "JPY", "CAD", "AUD",
})


def derive_search(purchase: dict, *, overlay: dict | None = None) -> str | None:
    """Return a Gmail query string for this Purchase, or None if no email
    receipt is expected.

    `overlay` carries the operator-specific config that pins which vendor
    names map to which Gmail sender addresses. The base skill itself
    knows nothing about specific airlines, hotels, or rideshare brands.

    Required overlay shape (all optional; sensible defaults below):

      receipt_match:
        on_site_vendor_hints: ["hotel <name>", "tmp.77", ...]   # → None / "paid on site"
        airline_vendor_to_sender:                               # vendor-name substring → Gmail from:
          "united":   "Receipts@united.com"
          "lufthansa": "lufthansa.com"
        rideshare_vendor_to_sender:                             # vendor → Gmail from: + extra hint
          "lyft":  "no-reply@lyftmail.com"
          "uber":  "noreply@uber.com"
        airport_codes_to_ignore: ["SFO", "JFK", ...]            # so they aren't matched as conf numbers
    """
    cfg = ((overlay or {}).get("receipt_match")) or {}
    on_site_hints = tuple(s.lower() for s in cfg.get("on_site_vendor_hints", []))
    airline_map = {k.lower(): v for k, v in (cfg.get("airline_vendor_to_sender") or {}).items()}
    rideshare_map = {k.lower(): v for k, v in (cfg.get("rideshare_vendor_to_sender") or {}).items()}
    conf_stopwords = _CONF_STOPWORDS | frozenset(
        s.upper() for s in cfg.get("airport_codes_to_ignore", [])
    )

    line_desc = ((purchase.get("Line") or [{}])[0]).get("Description") or ""
    vendor = (purchase.get("EntityRef") or {}).get("name", "").strip()
    vlow = vendor.lower()
    txn_date = purchase.get("TxnDate")  # YYYY-MM-DD

    # 0. On-site vendors → no Gmail receipt expected
    if any(h in vlow for h in on_site_hints):
        return None

    # 1. Airline → look up its Gmail sender + use confirmation number
    for needle, sender in airline_map.items():
        if needle in vlow:
            for m in _CONF_RE.finditer(line_desc):
                tok = m.group(1)
                if tok in conf_stopwords:
                    continue
                return f"from:{sender} {tok}"
            # No conf number → narrow to date window with the sender
            if txn_date:
                return f"from:{sender} after:{txn_date} before:{_next_day(txn_date)}"
            return None

    # 2. Rideshare → look up its Gmail sender + extract driver-name hint
    for needle, sender in rideshare_map.items():
        if needle in vlow:
            m = re.search(r"with\s+([A-Z][a-z]+)", line_desc)
            driver = m.group(1) if m else None
            if driver:
                return f'from:{sender} "ride with {driver}"'
            if txn_date:
                return f'from:{sender} after:{txn_date} before:{_next_day(txn_date)}'
            return None

    # 3. Uber ride: narrow Uber receipts to the TxnDate
    if "uber" in vlow:
        return f'from:noreply@uber.com after:{txn_date} before:{_next_day(txn_date)} subject:"trip with Uber"'

    return None


def _next_day(yyyy_mm_dd: str) -> str:
    from datetime import date, timedelta
    d = date.fromisoformat(yyyy_mm_dd) + timedelta(days=1)
    return d.isoformat()


def build_index_md(rows: list[dict]) -> str:
    """Render the index.md content from the per-purchase rows the runner built."""
    lines = [
        "# Receipts ↔ Purchases\n",
        "Each row is one QB Purchase marked for this trip. The Folder column",
        "points to the downloaded Gmail thread(s) covering that line item.",
        "",
        "| QB Purchase | Date | Amount | Vendor | Folder | Threads | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        folder = r.get("folder", "—")
        nthreads = len(r.get("threads") or [])
        status = r.get("receipt_status", "fetched")
        lines.append(
            f"| {r['purchase_id']} | {r['date']} | {r['amount']} {r['currency']} | "
            f"{r['vendor']} | {folder} | {nthreads} | {status} |"
        )
    lines.append("")
    lines.append("## Per-purchase detail\n")
    for r in rows:
        lines.append(f"### Purchase {r['purchase_id']} — {r['vendor']}  ({r['amount']} {r['currency']})")
        lines.append(f"- TxnDate: {r['date']}")
        lines.append(f"- Description: {r['desc']!r}")
        if r.get("receipt_status") == "no_email_receipt_expected":
            lines.append("- **No email receipt expected** — paid on site, attach photo manually.")
        else:
            lines.append(f"- Gmail query: `{r.get('search', '<none derived>')}`")
            if r.get("folder"):
                lines.append(f"- Folder: `{r['folder']}/`")
            for t in r.get("threads") or []:
                files_str = (", files: " + ", ".join(t["files"])) if t.get("files") else ""
                lines.append(f"  - `{t['thread_id']}/` — {t['subject']!r}{files_str}")
        lines.append("")
    return "\n".join(lines)
