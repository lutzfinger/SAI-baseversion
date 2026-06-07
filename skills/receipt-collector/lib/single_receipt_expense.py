"""Single-receipt expense filing (operator decision 2026-06-06).

For an operator forward like "cost while I was in Ithaca - for Cornell" + a receipt photo,
file ONE QuickBooks expense (Purchase) for the receipt and attach the image, with the
customer recorded in the memo (billable CustomerRef needs QB Plus; build_purchase already
handles this). This SUPERSEDES the 2026-05-20 invoice-only rule FOR single-receipt forwards
(operator: "a cost in QB with the receipt attached and customer Cornell").

Autonomous: operator->sai@ is the authorization. SAFE: behind a dry-run flag
``SAI_COST_COMPILER_EXPENSE_LIVE`` — default logs the would-be Purchase + attach WITHOUT
writing to QB; set =1 to write for real (mirrors invoice_fwd's dry-run cutover). FAIL-CLOSED
(#6): a missing customer / expense account / payment account -> no booking + a clear reason.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from lib.purchases import build_purchase

# "... for Cornell", "for Cornell University", "for the Acme Corp" -> the customer hint.
_FOR_CUSTOMER = re.compile(
    r"\bfor\s+(?:the\s+)?([A-Z][A-Za-z0-9&.\-']*(?:\s+[A-Z][A-Za-z0-9&.\-']*){0,4})"
)


def extract_customer_hint(text: str) -> Optional[str]:
    """Pull the billable customer from an operator forward like 'cost ... for Cornell'.
    Returns None when there's no clear 'for <Customer>' phrase (then the trip-compile agent
    handles it instead of this single-receipt path)."""
    match = _FOR_CUSTOMER.search(text or "")
    if not match:
        return None
    hint = match.group(1).strip(" .-")
    return hint or None


def build_trip_slug(customer_hint: str | None, date_iso: str | None) -> str:
    cust = re.sub(r"[^a-z0-9]+", "-", (customer_hint or "expense").lower()).strip("-")
    year_month = (date_iso or "")[:7]
    return f"{cust}-{year_month}" if len(year_month) == 7 else (cust or "expense")


def expense_live() -> bool:
    return os.environ.get("SAI_COST_COMPILER_EXPENSE_LIVE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


@dataclass
class ReceiptExtraction:
    """The fields read off the receipt image (vision) + the operator text."""

    amount: float
    date: str           # ISO yyyy-mm-dd, from the receipt
    vendor: str
    currency: str = "USD"
    description: str | None = None


def pick_payment_account(accounts: list[dict]) -> Optional[dict]:
    """A credit-card account for the Purchase AccountRef; fall back to a bank account."""
    for a in accounts:
        if a.get("AccountSubType") == "CreditCard" or a.get("AccountType") == "Credit Card":
            return a
    for a in accounts:
        if a.get("AccountType") == "Bank":
            return a
    return None


def pick_expense_account(accounts: list[dict], category_hint: str | None = None) -> Optional[dict]:
    """An Expense account for the line. Prefer a hint match, then Meals/Travel, then any."""
    expenses = [a for a in accounts if a.get("Classification") == "Expense"]
    if category_hint:
        hint = category_hint.lower()
        for a in expenses:
            if hint in a.get("Name", "").lower():
                return a
    for keyword in ("meals", "travel", "entertainment", "dining"):
        for a in expenses:
            if keyword in a.get("Name", "").lower():
                return a
    return expenses[0] if expenses else None


def build_expense_spec(
    *, extraction: ReceiptExtraction, vendor: dict | None,
    expense_account: dict, payment_account: dict,
) -> dict[str, Any]:
    return {
        "name": extraction.description or extraction.vendor or "Expense",
        "payment_account": payment_account.get("Id", ""),
        "vendor_id": (vendor or {}).get("Id", ""),
        "vendor_name": (vendor or {}).get("DisplayName", extraction.vendor or "Unknown vendor"),
        "date": extraction.date,
        "currency": extraction.currency,
        "amount": extraction.amount,
        "account_id": expense_account.get("Id", ""),
        "account_name": expense_account.get("Name", ""),
    }


def file_single_receipt_expense(
    *,
    extraction: ReceiptExtraction,
    customer_hint: str | None,
    qb_client: Any,
    receipt_path: str | None,
    trip_slug: str,
    marker: str,
    category_hint: str | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """Resolve customer/accounts/vendor, build the expense, then dry-run-log or create it +
    attach the receipt. Returns a summary the caller turns into an operator reply."""
    if dry_run is None:
        dry_run = not expense_live()

    accounts = qb_client.list_accounts()
    payment_account = pick_payment_account(accounts)
    expense_account = pick_expense_account(accounts, category_hint)

    customer = qb_client.find_customer_by_name(customer_hint) if customer_hint else None
    if customer_hint and customer is None:
        return {"status": "customer_not_found", "customer_hint": customer_hint, "dry_run": dry_run}
    if expense_account is None:
        return {"status": "no_expense_account", "dry_run": dry_run}
    if payment_account is None:
        return {"status": "no_payment_account", "dry_run": dry_run}

    vendor = qb_client.find_vendor_by_name(extraction.vendor) if extraction.vendor else None
    customer_name = (customer or {}).get("DisplayName") or customer_hint
    spec = build_expense_spec(
        extraction=extraction, vendor=vendor,
        expense_account=expense_account, payment_account=payment_account,
    )
    purchase_obj = build_purchase(spec, marker, customer_name=customer_name)

    summary: dict[str, Any] = {
        "amount": extraction.amount, "currency": extraction.currency, "date": extraction.date,
        "vendor": spec["vendor_name"], "customer": customer_name,
        "expense_account": expense_account.get("Name", ""),
        "payment_account": payment_account.get("Name", ""),
        "dry_run": dry_run, "purchase_id": None, "attached": False,
        "purchase_obj": purchase_obj,
    }
    if dry_run:
        summary["status"] = "would_book"
        return summary

    result = qb_client.create_purchase(purchase_obj)
    purchase_id = result.get("Id")
    summary["purchase_id"] = purchase_id
    summary["status"] = "booked"
    if receipt_path and purchase_id:
        from lib import qb_attachments

        try:
            qb_attachments.upload_for_purchase(
                qb_client, purchase_id, receipt_path, trip_slug, note_prefix="receipt"
            )
            summary["attached"] = True
        except Exception as exc:  # noqa: BLE001 — attach failure must not lose the booked expense
            summary["attach_error"] = str(exc)
    return summary


def reply_text(summary: dict[str, Any]) -> str:
    """Operator-facing message from the summary."""
    status = summary.get("status")
    if status == "customer_not_found":
        return (
            f"I read the receipt, but I couldn't find a QuickBooks customer matching "
            f"'{summary.get('customer_hint')}', so I didn't book anything (I won't guess a "
            "customer). Tell me the exact QB customer name and I'll file it."
        )
    if status in ("no_expense_account", "no_payment_account"):
        return (
            "I read the receipt but couldn't resolve a QuickBooks expense/payment account, "
            "so I didn't book anything. Please check the QB chart of accounts."
        )
    amount = f"{summary.get('currency', '')} {summary.get('amount')}".strip()
    head = (
        f"{amount} expense at {summary.get('vendor')} on {summary.get('date')}, "
        f"account {summary.get('expense_account')}, billed to {summary.get('customer')}"
    )
    if summary.get("dry_run"):
        return (
            f"DRY RUN — I would book: {head}, with the receipt attached. "
            "Nothing was written to QuickBooks. Flip SAI_COST_COMPILER_EXPENSE_LIVE=1 to file it."
        )
    attached = "receipt attached" if summary.get("attached") else "receipt NOT attached (see logs)"
    return f"Booked: {head} (Purchase {summary.get('purchase_id')}), {attached}."


__all__ = [
    "ReceiptExtraction", "expense_live", "pick_payment_account", "pick_expense_account",
    "build_expense_spec", "file_single_receipt_expense", "reply_text",
]
