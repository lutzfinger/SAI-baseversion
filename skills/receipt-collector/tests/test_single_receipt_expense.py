"""Single-receipt expense filing (operator decision 2026-06-06: expense + attach).

Covers: dry-run logs the would-be Purchase without writing; live writes + attaches; the
customer is recorded; fail-closed when the customer / accounts can't be resolved.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.single_receipt_expense import (  # noqa: E402
    ReceiptExtraction,
    build_trip_slug,
    extract_customer_hint,
    file_single_receipt_expense,
    pick_expense_account,
    pick_payment_account,
    reply_text,
)


def test_extract_customer_hint():
    assert extract_customer_hint("cost while I was in Ithaca - for Cornell") == "Cornell"
    assert extract_customer_hint("expenses for Cornell University this trip") == "Cornell University"
    assert extract_customer_hint("just a normal note, no customer") is None


def test_build_trip_slug():
    assert build_trip_slug("Cornell", "2026-03-01") == "cornell-2026-03"
    assert build_trip_slug("Cornell University", "2026-03-15") == "cornell-university-2026-03"

_ACCOUNTS = [
    {"Id": "10", "Name": "Checking", "AccountType": "Bank", "Classification": "Asset"},
    {"Id": "20", "Name": "Amex Card", "AccountType": "Credit Card",
     "AccountSubType": "CreditCard", "Classification": "Liability"},
    {"Id": "30", "Name": "Meals and Entertainment", "Classification": "Expense"},
    {"Id": "31", "Name": "Office Supplies", "Classification": "Expense"},
]


class _FakeQB:
    def __init__(self, customer=True, vendor=True, existing_marker=None):
        self._customer = {"Id": "C1", "DisplayName": "Cornell University"} if customer else None
        self._vendor = {"Id": "V1", "DisplayName": "Tavern on the Commons"} if vendor else None
        self._existing_marker = existing_marker
        self.created = []

    def list_accounts(self, classification=None):
        return _ACCOUNTS

    def find_customer_by_name(self, name):
        return self._customer

    def find_vendor_by_name(self, name):
        return self._vendor

    def find_purchase_by_marker(self, marker, start, end):
        return {"Id": "EXISTING-PUR"} if self._existing_marker == marker else None

    def create_purchase(self, obj):
        self.created.append(obj)
        return {"Id": "PUR-1"}


def _extraction():
    return ReceiptExtraction(
        amount=149.95, date="2026-03-01", vendor="Tavern on the Commons",
        currency="USD", description="Dinner",
    )


# ── account pickers ───────────────────────────────────────────────────────────

def test_pick_payment_prefers_credit_card():
    assert pick_payment_account(_ACCOUNTS)["Id"] == "20"


def test_pick_expense_prefers_meals():
    assert pick_expense_account(_ACCOUNTS)["Id"] == "30"


# ── dry-run (default) writes NOTHING ─────────────────────────────────────────

def test_dry_run_does_not_write_but_proposes_correct_expense():
    qb = _FakeQB()
    out = file_single_receipt_expense(
        extraction=_extraction(), customer_hint="Cornell", qb_client=qb,
        receipt_path=None, trip_slug="cornell-2026-03", marker="[sai-receipt:test]",
        dry_run=True,
    )
    assert out["status"] == "would_book" and out["dry_run"] is True
    assert qb.created == []  # nothing written to QB
    assert out["amount"] == 149.95 and out["customer"] == "Cornell University"
    assert out["expense_account"] == "Meals and Entertainment"
    # the Purchase obj carries the amount + customer-in-memo
    obj = out["purchase_obj"]
    assert obj["Line"][0]["Amount"] == 149.95
    assert "Cornell University" in obj["PrivateNote"]
    assert "DRY RUN" in reply_text(out)


# ── live writes the Purchase ──────────────────────────────────────────────────

def test_live_creates_purchase():
    qb = _FakeQB()
    out = file_single_receipt_expense(
        extraction=_extraction(), customer_hint="Cornell", qb_client=qb,
        receipt_path=None, trip_slug="cornell-2026-03", marker="[sai-receipt:test]",
        dry_run=False,
    )
    assert out["status"] == "booked" and out["purchase_id"] == "PUR-1"
    assert len(qb.created) == 1
    assert "Booked:" in reply_text(out)


# ── fail-closed ───────────────────────────────────────────────────────────────

def test_idempotent_skips_when_marker_already_booked():
    qb = _FakeQB(existing_marker="[sai-receipt:test]")
    out = file_single_receipt_expense(
        extraction=_extraction(), customer_hint="Cornell", qb_client=qb,
        receipt_path=None, trip_slug="x", marker="[sai-receipt:test]", dry_run=False,
    )
    assert out["status"] == "already_booked" and out["purchase_id"] == "EXISTING-PUR"
    assert qb.created == []  # never double-books


def test_fail_closed_when_customer_not_found():
    qb = _FakeQB(customer=False)
    out = file_single_receipt_expense(
        extraction=_extraction(), customer_hint="Cornell", qb_client=qb,
        receipt_path=None, trip_slug="x", marker="m", dry_run=False,
    )
    assert out["status"] == "customer_not_found"
    assert qb.created == []  # nothing booked without a customer
    assert "couldn't find a QuickBooks customer" in reply_text(out)
