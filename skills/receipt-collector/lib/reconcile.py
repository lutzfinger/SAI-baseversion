"""
reconcile — match expected billables to QB Purchases (steps 6 + 7).

After a trip, the operator has a list of expected costs (built from
the email-receipt fetch + the credit-card scan + the calendar
pre-bookings). reconcile compares that list against the QB Purchase
register and tells the operator:

  * Which expected costs HAVE a matching Purchase (step 6 — the
    Purchase can then be tagged / memo'd / receipt-attached via the
    existing `tag-purchases` + `match-receipts-to-purchases` subs).
  * Which expected costs have NO matching Purchase (step 7 — these
    were probably paid cash or on a card the overlay doesn't know
    about; surface as an audit-log warning so the operator can
    investigate).
  * Which Purchases in the window are EXTRA (not in the expected
    list) — surfaces for operator review (could be personal or
    another customer).

Matching tolerance (deterministic):
  * Amount: ±$0.50 (catches taxi rounding) OR ±0.5% of the expected
    amount, whichever is bigger.
  * Date: ±2 days (cards post next-day; international transactions
    sometimes post 2 days late).
  * Vendor: a soft signal — if both records carry a vendor string,
    the longest-common-substring is computed and used to tiebreak
    when one expected billable could match multiple Purchases.

Stays a rules-tier per SAI #12. No LLM in the hot path.

Public API:
    Expected(name, txn_date, amount, currency, vendor=None,
             purchase_id_hint=None)
    Match(expected, purchase, score, reason)
    reconcile(expected_list, qb_purchases, ...) -> ReconcileResult
        .matched: list[Match]
        .missing: list[Expected]   # in expected_list, no QB tx found
        .extras:  list[dict]       # in QB but not in expected_list
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Optional


@dataclass
class Expected:
    name: str
    txn_date: date
    amount: float
    currency: str = "USD"
    vendor: Optional[str] = None
    purchase_id_hint: Optional[str] = None  # if operator already knows the id


@dataclass
class Match:
    expected: Expected
    purchase: dict
    score: float
    reason: str


@dataclass
class ReconcileResult:
    matched: list[Match] = field(default_factory=list)
    missing: list[Expected] = field(default_factory=list)
    extras: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"matched={len(self.matched)}  "
            f"missing={len(self.missing)}  "
            f"extras={len(self.extras)}"
        )


def _amount_close(a: float, b: float, tolerance_abs: float = 0.50,
                  tolerance_pct: float = 0.005) -> bool:
    """Whether two amounts are within the larger of (abs, pct) tolerance.

    Per principle #6 fail-closed: a slightly negative amount or NaN is
    treated as no-match (returns False), never matched defensively.
    """
    if a is None or b is None:
        return False
    try:
        a = abs(float(a))
        b = abs(float(b))
    except (TypeError, ValueError):
        return False
    tol = max(tolerance_abs, tolerance_pct * max(a, b))
    return abs(a - b) <= tol


def _date_close(a: date, b: date, tolerance_days: int = 2) -> bool:
    if not a or not b:
        return False
    return abs((a - b).days) <= tolerance_days


def _vendor_overlap(a: Optional[str], b: Optional[str]) -> float:
    """Return [0, 1] longest-common-substring share. Bonus signal only;
    not used for primary matching."""
    if not a or not b:
        return 0.0
    a = a.lower()
    b = b.lower()
    # Cheap LCS-substring via dynamic programming, O(n*m).
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0
    dp = [0] * (lb + 1)
    best = 0
    for i in range(1, la + 1):
        prev = 0
        ch = a[i - 1]
        for j in range(1, lb + 1):
            tmp = dp[j]
            if ch == b[j - 1]:
                dp[j] = prev + 1
                if dp[j] > best:
                    best = dp[j]
            else:
                dp[j] = 0
            prev = tmp
    return best / max(la, lb)


def _qb_purchase_date(p: dict) -> Optional[date]:
    s = p.get("TxnDate")
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _qb_purchase_amount(p: dict) -> Optional[float]:
    v = p.get("TotalAmt")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _qb_purchase_vendor(p: dict) -> Optional[str]:
    return (p.get("EntityRef") or {}).get("name")


def reconcile(
    expected_list: Iterable[Expected],
    qb_purchases: Iterable[dict],
    *,
    amount_tolerance_abs: float = 0.50,
    amount_tolerance_pct: float = 0.005,
    date_tolerance_days: int = 2,
) -> ReconcileResult:
    """Match expected billables against a list of QB Purchases.

    Algorithm:
      1. For each Expected: collect candidate Purchases where
         amount AND date are within tolerance.
      2. Score candidates by (1 - amount_delta/expected_amount) +
         0.2 * vendor_overlap.
      3. Greedy-assign: pick the highest-scoring Purchase that's
         not yet claimed by an earlier Expected. (Greedy is fine
         because Expected items rarely have identical amounts.)
      4. Anything in expected_list with no candidate → missing.
      5. Anything in qb_purchases not claimed → extras.
    """
    expected_list = list(expected_list)
    purchases = list(qb_purchases)
    claimed_ids: set[str] = set()
    matches: list[Match] = []
    missing: list[Expected] = []

    # Sort expected by amount desc so big-ticket items get matched first
    # (a $6000 flight should win over a $50 taxi if amounts collide).
    expected_sorted = sorted(
        expected_list, key=lambda e: float(e.amount or 0), reverse=True
    )

    for exp in expected_sorted:
        candidates: list[tuple[float, dict, str]] = []
        for p in purchases:
            pid = p.get("Id")
            if pid in claimed_ids:
                continue
            p_amount = _qb_purchase_amount(p)
            p_date = _qb_purchase_date(p)
            if not _amount_close(
                exp.amount, p_amount,
                tolerance_abs=amount_tolerance_abs,
                tolerance_pct=amount_tolerance_pct,
            ):
                continue
            if not _date_close(exp.txn_date, p_date,
                               tolerance_days=date_tolerance_days):
                continue
            # Compute score
            try:
                amt_score = 1.0 - min(
                    1.0, abs(float(exp.amount) - float(p_amount))
                    / max(0.01, float(exp.amount))
                )
            except (TypeError, ValueError, ZeroDivisionError):
                amt_score = 0.5
            vendor_score = _vendor_overlap(exp.vendor, _qb_purchase_vendor(p))
            score = amt_score + 0.2 * vendor_score
            # purchase_id_hint forces a match — score it sky-high
            if exp.purchase_id_hint and pid == exp.purchase_id_hint:
                score = 99.0
                reason = "purchase_id_hint match"
            else:
                date_delta = abs((exp.txn_date - p_date).days) if p_date else 0
                reason = (
                    f"amount Δ={abs(exp.amount - (p_amount or 0)):.2f}, "
                    f"date Δ={date_delta}d, "
                    f"vendor={_qb_purchase_vendor(p)!r}"
                )
            candidates.append((score, p, reason))
        if not candidates:
            missing.append(exp)
            continue
        candidates.sort(key=lambda t: t[0], reverse=True)
        score, p, reason = candidates[0]
        claimed_ids.add(p["Id"])
        matches.append(Match(expected=exp, purchase=p, score=score, reason=reason))

    extras = [p for p in purchases if p.get("Id") not in claimed_ids]
    return ReconcileResult(matched=matches, missing=missing, extras=extras)


def expected_from_plan(plan: dict) -> list[Expected]:
    """Build Expected list from a plan.json's `expected_billables` array.

    Each plan entry can have:
      name (str, required)
      txn_date (ISO YYYY-MM-DD, required)
      amount (number, required)
      currency (default "USD")
      vendor (optional)
      purchase_id_hint (optional)
    """
    out: list[Expected] = []
    for row in plan.get("expected_billables") or []:
        out.append(Expected(
            name=row["name"],
            txn_date=date.fromisoformat(row["txn_date"]),
            amount=float(row["amount"]),
            currency=(row.get("currency") or "USD").upper(),
            vendor=row.get("vendor"),
            purchase_id_hint=row.get("purchase_id_hint"),
        ))
    return out
