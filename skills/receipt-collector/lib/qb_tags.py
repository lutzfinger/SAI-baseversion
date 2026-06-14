"""
QB tag + memo operations on Purchases.

The QBO v3 REST API does NOT expose the `Tag` entity (verified 2026-05-20 —
both `SELECT * FROM Tag` and `GET /tag` return "Unsupported Operation").
The Tags feature is UI-only on the operator's plan.

This module therefore does the only piece the public API supports:

  * append a "Billed as expenses to <customer>" line to PrivateNote
  * idempotently (re-runs are no-ops if the line is already present)

The runner's `tag-purchases` subcommand also PRINTS the list of Purchase IDs
so the operator can paste the tag into the QB UI in one batch.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Iterable


BILLED_MEMO_TEMPLATE = "Billed as expenses to {customer}"


def find_purchases_for_trip(
    client,
    trip_slug: str,
    window_start: date,
    window_end: date,
) -> list[dict]:
    """Return Purchases whose PrivateNote contains the trip marker.

    The marker format matches the one written by lib.purchases.build_purchase:
        [sai-receipts:<trip_slug>] <line_name>
    Any Purchase whose marker mentions the slug counts as "for this trip".
    """
    q = (
        f"SELECT * FROM Purchase "
        f"WHERE TxnDate >= '{window_start.isoformat()}' "
        f"AND TxnDate <= '{window_end.isoformat()}' MAXRESULTS 500"
    )
    resp = client._request(
        "GET",
        f"/v3/company/{client.realm}/query",
        params={"query": q, "minorversion": "75"},
    )
    rows = resp.json().get("QueryResponse", {}).get("Purchase", [])
    marker_prefix = f"[sai-receipts:{trip_slug}]"
    return [p for p in rows if marker_prefix in (p.get("PrivateNote") or "")]


def already_billed(purchase: dict, customer_name: str) -> bool:
    """Idempotency check: is this purchase already memo-flagged?"""
    note = purchase.get("PrivateNote") or ""
    return BILLED_MEMO_TEMPLATE.format(customer=customer_name) in note


def mark_billed(client, purchase: dict, customer_name: str) -> dict:
    """Append the 'Billed as expenses to <customer>' line to PrivateNote.

    No-op if the line is already present. Returns the (possibly unchanged)
    Purchase object.
    """
    if already_billed(purchase, customer_name):
        return purchase
    note = purchase.get("PrivateNote") or ""
    new_note = (
        note.rstrip() + "\n\n" + BILLED_MEMO_TEMPLATE.format(customer=customer_name)
    ).strip()
    body = {**purchase, "PrivateNote": new_note, "sparse": True}
    resp = client._request(
        "POST",
        f"/v3/company/{client.realm}/purchase",
        params={"minorversion": "75"},
        data=json.dumps(body),
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"mark_billed failed Id={purchase['Id']}: {resp.status_code} {resp.text[:300]}")
    return resp.json().get("Purchase", purchase)


def remove_trip_marker(client, purchase: dict, trip_slug: str, reason: str = "") -> dict:
    """Strip the [sai-receipts:<trip>] marker so this Purchase no longer
    appears in find_purchases_for_trip() results.

    Used when an item was originally marked for a trip but later reclassified
    as non-billable (e.g., a personal-detour leg).
    """
    note = purchase.get("PrivateNote") or ""
    marker_prefix = f"[sai-receipts:{trip_slug}]"
    if marker_prefix not in note:
        return purchase
    # Strip every line that contains the marker
    kept = [ln for ln in note.splitlines() if marker_prefix not in ln]
    new_note = "\n".join(kept).strip()
    if reason:
        new_note = (new_note + f"\n\n[sai-reclassified] {reason}").strip()
    body = {**purchase, "PrivateNote": new_note, "sparse": True}
    resp = client._request(
        "POST",
        f"/v3/company/{client.realm}/purchase",
        params={"minorversion": "75"},
        data=json.dumps(body),
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"remove_trip_marker failed Id={purchase['Id']}: {resp.status_code} {resp.text[:300]}")
    return resp.json().get("Purchase", purchase)


def manual_tag_report(purchases: Iterable[dict], tag_name: str) -> str:
    """Build the printed report the operator pastes into QB UI.

    The QB v3 API has no Tag write endpoint, so the operator must add the
    tag manually. This output is a focused list — Purchase Id, date,
    amount, vendor, line description — so the operator can find each
    Purchase fast.
    """
    lines = [
        f"MANUAL STEP — add tag {tag_name!r} to these {len(list(purchases))} Purchases",
        "in the QB UI (Expenses → click row → Tags field):",
        "",
    ]
    purchases = list(purchases)  # re-materialise after iterating for len
    lines[0] = f"MANUAL STEP — add tag {tag_name!r} to these {len(purchases)} Purchases"
    for p in sorted(purchases, key=lambda x: (x.get("TxnDate") or "", x.get("Id"))):
        vname = (p.get("EntityRef") or {}).get("name", "?")
        cur = (p.get("CurrencyRef") or {}).get("value", "?")
        amt = p.get("TotalAmt")
        desc = ((p.get("Line") or [{}])[0]).get("Description") or ""
        lines.append(
            f"  Purchase Id={p['Id']}  {p.get('TxnDate')}  {amt} {cur}  vendor={vname!r}\n"
            f"    {desc[:90]}"
        )
    return "\n".join(lines)
