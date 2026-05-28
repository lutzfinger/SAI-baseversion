"""
Purchase entity builders (atomic, base skill).

Pure functions that build QBO Purchase JSON payloads from a flat dict.
The runner / overlay binds these to a QBClient.

Public API (atomic):
    build_purchase(spec, marker, customer_name=None)
    untag_customer(purchase_obj, customer_name)  - returns modified copy

A `spec` is a flat dict with these keys:
    name             - human-readable label for this purchase
    date             - ISO date string
    vendor_id        - QBO Vendor.Id
    vendor_name      - QBO Vendor.DisplayName
    amount           - numeric, in the row's currency
    currency         - "USD" / "EUR" / etc.
    account_id       - QBO Account.Id (the EXPENSE account, e.g. "Travel:Airfare")
    account_name     - QBO Account.FullyQualifiedName (cosmetic)
    payment_account  - QBO Account.Id of the credit-card/bank used to pay
    memo             - free-text private note

A `marker` is a unique string we drop into PrivateNote so we can find the
purchase again on a re-run (idempotency).
"""
from __future__ import annotations


def build_purchase(spec: dict, marker: str, customer_name: str | None = None) -> dict:
    """Build a QBO Purchase JSON body. No billable-to-customer tagging (Plus-tier
    feature). The customer link is recorded only in the memo for the
    bookkeeper's reference and matched up via a separate Invoice.
    """
    note_lines = [spec.get("memo", "")]
    if customer_name:
        note_lines.append(f"Billed to: {customer_name} (separate Invoice).")
    note_lines.append(marker)
    private_note = "\n\n".join(p for p in note_lines if p)

    desc = spec["name"]
    if customer_name:
        desc = f"{desc} (billed to {customer_name} via separate Invoice)"

    return {
        "PaymentType": "CreditCard",
        "AccountRef": {"value": spec["payment_account"]},
        "EntityRef": {"value": spec["vendor_id"], "name": spec["vendor_name"], "type": "Vendor"},
        "TxnDate": spec["date"],
        "CurrencyRef": {"value": spec["currency"]},
        "PrivateNote": private_note,
        "Line": [
            {
                "Amount": spec["amount"],
                "DetailType": "AccountBasedExpenseLineDetail",
                "Description": desc,
                "AccountBasedExpenseLineDetail": {
                    "AccountRef": {"value": spec["account_id"], "name": spec.get("account_name", "")},
                },
            }
        ],
    }


def untag_customer(purchase_obj: dict, customer_name: str) -> dict:
    """Return a sparse-update body that strips the customer reference
    from a Purchase's PrivateNote and line description. Keeps the
    expense itself intact.
    """
    note = purchase_obj.get("PrivateNote") or ""
    new_note = note.replace(
        f"Billed to: {customer_name} (separate Invoice).",
        f"NOT billed to {customer_name} (reclassified). Stays as business-travel cost.",
    )
    new_lines = []
    for ln in purchase_obj.get("Line", []):
        ln = dict(ln)
        desc = ln.get("Description") or ""
        if f"billed to {customer_name}" in desc.lower():
            ln["Description"] = (
                desc.split(" (billed to")[0]
                + f" (NOT billable to {customer_name})"
            )
        new_lines.append(ln)
    return {**purchase_obj, "PrivateNote": new_note, "Line": new_lines}
