"""
Invoice builder (atomic, base skill).

Pure functions: build a QBO Invoice JSON payload from a list of line
specs + a customer.

Public API (atomic):
    build_invoice(customer, lines, currency, marker, due_in_days=30,
                  po_number=None, header_memo=None,
                  on_fx_log=None, fx_fallback_table=None)
    build_invoice_line(line, invoice_currency=None, on_fx_log=None,
                       fx_fallback_table=None)
    add_line_to_invoice(inv_obj, new_line, invoice_currency=None, ...)
    drop_lines_matching(inv_obj, predicate)

Line spec dict (all keys):
    item_id        - QBO Item.Id (e.g. "Air Ticket", "Hotels", "Taxi")
    item_name      - QBO Item.Name
    qty            - quantity (numeric)
    rate           - unit price in the SOURCE currency (or invoice
                     currency if `source_currency` is absent)
    description    - free-text shown on the invoice
    purchase_id    - OPTIONAL: QB Purchase Id this line is linked to
                     (used in audit log so we can correlate the rate
                     write-back with the originating Purchase)
    source_currency - OPTIONAL: the currency of the original receipt
                     (e.g. "USD" for a card swipe in dollars). If
                     present AND != invoice_currency, the line is
                     converted at runtime via fx_live (Frankfurter
                     ECB historical rates) and a "[FX: ...]" note
                     is appended to the description.
    txn_date       - OPTIONAL: ISO date "YYYY-MM-DD" the cost was
                     incurred. Required when source_currency is set
                     so the FX lookup uses the right historical rate.

If `source_currency` is omitted, the line is taken as already being
in invoice currency (legacy behavior — no FX work happens).

The FX call site is logged via the optional `on_fx_log` callback so
the caller (runner) can write each lookup to the audit JSONL.

If the live FX call raises (network down, weekend, missing pair),
the builder falls back to `fx_fallback_table` (a flat
{"USD_EUR": 0.92, ...} dict from the overlay). If neither path
produces a rate, the line raises — fail closed per principle #6.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable


def _resolve_fx(
    from_ccy: str,
    to_ccy: str,
    on_date: date,
    fallback_table: dict | None,
) -> tuple[float, str]:
    """Return (rate, source) for from_ccy → to_ccy on on_date.

    source is "live" (Frankfurter/ECB), "static" (overlay fallback table),
    or raises ValueError if neither is available.
    """
    if from_ccy.upper() == to_ccy.upper():
        return 1.0, "identity"

    # Try live first.
    try:
        from lib import fx_live  # local import to keep base testable
        rate = fx_live.get_rate(from_ccy, to_ccy, on_date)
        return rate, "live"
    except Exception:
        pass

    # Fall back to the static table in the overlay.
    if fallback_table:
        key = f"{from_ccy.upper()}_{to_ccy.upper()}"
        if key in fallback_table:
            return float(fallback_table[key]), "static"

    raise ValueError(
        f"No FX rate available for {from_ccy}→{to_ccy} on {on_date.isoformat()}: "
        f"live lookup failed AND no fallback entry under {from_ccy.upper()}_{to_ccy.upper()}."
    )


def build_invoice_line(
    line: dict,
    invoice_currency: str | None = None,
    on_fx_log: Callable[[dict], None] | None = None,
    fx_fallback_table: dict | None = None,
) -> dict:
    src_ccy = (line.get("source_currency") or "").upper() or None
    inv_ccy = (invoice_currency or "").upper() or None
    rate = float(line["rate"])
    qty = float(line.get("qty", 1.0))
    description = line.get("description") or ""

    fx_applied = False
    if src_ccy and inv_ccy and src_ccy != inv_ccy:
        txn_date_str = line.get("txn_date")
        if not txn_date_str:
            raise ValueError(
                f"Line with source_currency={src_ccy!r} must also include "
                f"txn_date (ISO YYYY-MM-DD) so FX lookup can use the "
                f"correct historical rate."
            )
        txn_date = date.fromisoformat(txn_date_str)
        fx_rate, source = _resolve_fx(src_ccy, inv_ccy, txn_date, fx_fallback_table)
        original_rate = rate
        rate = round(rate * fx_rate, 4)
        description = (description.rstrip()
                       + f"\n[FX: {fx_rate:.4f} {src_ccy}→{inv_ccy} @ "
                       + f"{txn_date.isoformat()} ({source})]").lstrip()
        fx_applied = True
        if on_fx_log:
            on_fx_log({
                "from_ccy": src_ccy,
                "to_ccy": inv_ccy,
                "rate": fx_rate,
                "source": source,
                "on_date": txn_date.isoformat(),
                "original_unit_rate": original_rate,
                "converted_unit_rate": rate,
                "purchase_id": line.get("purchase_id"),
                "line_description": line.get("description"),
            })

    amount = round(qty * rate, 2)
    return {
        "DetailType": "SalesItemLineDetail",
        "Amount": amount,
        "Description": description,
        "SalesItemLineDetail": {
            "ItemRef": {"value": line["item_id"], "name": line["item_name"]},
            "Qty": qty,
            "UnitPrice": round(rate, 4),
        },
    }


def build_invoice(
    customer: dict,
    lines: list[dict],
    currency: str,
    marker: str,
    due_in_days: int = 30,
    po_number: str | None = None,
    header_memo: str | None = None,
    issue_date: date | None = None,
    on_fx_log: Callable[[dict], None] | None = None,
    fx_fallback_table: dict | None = None,
) -> dict:
    issue = issue_date or date.today()
    due = issue + timedelta(days=due_in_days)
    memo_lines = []
    if po_number:
        memo_lines.append(f"PO Number: {po_number}")
    if header_memo:
        memo_lines.append(header_memo)
    customer_memo = "\n\n".join(memo_lines) if memo_lines else ""
    return {
        "CustomerRef": {"value": customer["Id"], "name": customer["DisplayName"]},
        "CurrencyRef": {"value": currency},
        "TxnDate": issue.isoformat(),
        "DueDate": due.isoformat(),
        "PrivateNote": f"{header_memo or ''} {marker}".strip(),
        "CustomerMemo": {"value": customer_memo} if customer_memo else None,
        "Line": [
            build_invoice_line(
                ln,
                invoice_currency=currency,
                on_fx_log=on_fx_log,
                fx_fallback_table=fx_fallback_table,
            )
            for ln in lines
        ],
    }


def add_line_to_invoice(
    inv_obj: dict,
    new_line: dict,
    invoice_currency: str | None = None,
    on_fx_log: Callable[[dict], None] | None = None,
    fx_fallback_table: dict | None = None,
) -> dict:
    """Return a sparse-update body that appends a line to an existing invoice."""
    return {
        **inv_obj,
        "Line": list(inv_obj.get("Line", [])) + [
            build_invoice_line(
                new_line,
                invoice_currency=invoice_currency
                                 or (inv_obj.get("CurrencyRef") or {}).get("value"),
                on_fx_log=on_fx_log,
                fx_fallback_table=fx_fallback_table,
            )
        ],
    }


def drop_lines_matching(inv_obj: dict, predicate: Callable[[dict], bool]) -> dict:
    """Return a sparse-update body with all lines for which predicate(line) is True removed."""
    kept = [ln for ln in inv_obj.get("Line", []) if not predicate(ln)]
    return {**inv_obj, "Line": kept}
