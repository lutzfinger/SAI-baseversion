"""
Search Gmail for messages the operator forwarded to QuickBooks "Receipts"
inbox addresses, and pull their image/PDF attachments.

QBO's Receipts inbox accepts forwards at addresses like
`<company-prefix>+<wallet>@assist.intuit.com`. Operators routinely forward
on-site receipt photos (taxi, hotel, restaurant) to themselves with a
subject like "Charge <customer> taxi from <airport>" — labeling the trip and the
billable purpose inline.

This module pulls those threads + their attachments and lets the runner
match them to QB Purchases (by subject keywords / date / vendor) so each
photo lands on the right Purchase as an Attachable.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path


def build_query(
    receipts_inboxes: list[str],
    start: date,
    end: date,
    require_attachment: bool = True,
) -> str:
    """Build a Gmail query string for forwards to one of the QB receipts addresses."""
    inboxes_or = " OR ".join(f"to:{a}" for a in receipts_inboxes)
    parts = [f"({inboxes_or})"]
    if require_attachment:
        parts.append("has:attachment")
    parts.append(f"after:{start.strftime('%Y/%m/%d')}")
    parts.append(f"before:{(end).strftime('%Y/%m/%d')}")
    return " ".join(parts)


# Subject keywords that pin a forwarded receipt to a Purchase. The runner
# scores each (thread, purchase) pair on overlap between thread subject
# tokens and Purchase line-description tokens.
def subject_tokens(s: str) -> set[str]:
    """Lowercase alphanumeric tokens, dropped of trivial stop-words.

    Note: 'to' and 'from' are kept — they're the only disambiguator for
    directional travel ('taxi from cdg' vs 'taxi to CDG' point at
    different Purchases).
    """
    stop = {"the", "and", "for", "of", "a", "is", "in", "on", "at",
            "with", "your", "my", "i", "this", "that", "be", "as",
            "an", "or", "by", "via", "fwd", "re", "receipt", "invoice"}
    toks = re.findall(r"[a-z0-9]+", s.lower())
    return {t for t in toks if t and t not in stop and len(t) > 1}


_ARROW_RE = re.compile(
    r"([A-Za-z][A-Za-z ]*?[A-Za-z])\s*(?:→|->|to)\s+([A-Za-z][A-Za-z ]*?[A-Za-z])",
    re.IGNORECASE,
)
_FROM_RE = re.compile(r"\bfrom\s+([A-Za-z]+)", re.IGNORECASE)
_TO_RE = re.compile(r"\bto\s+([A-Za-z]+)", re.IGNORECASE)


def directional_score(thread_subject: str, line_desc: str) -> int:
    """Reward or punish a candidate match based on directional cues.

    Purchase line desc like "Taxi <Airport> → <Hotel>" sets
    origin=<Airport>, dest=<Hotel>. A thread subject "from <Airport>"
    agrees with origin (+5); "to <Airport>" disagrees with origin (-5)
    — strong enough to flip the winner when token overlap is tied.
    """
    m = re.search(r"(\w[\w ]*?)\s*→\s*(\w[\w ]*?)(?:\s*\(|$|,)", line_desc)
    if not m:
        return 0
    origin = m.group(1).lower().strip()
    dest = m.group(2).lower().strip()
    subj_lc = thread_subject.lower()

    score = 0
    fm = _FROM_RE.search(subj_lc)
    tm = _TO_RE.search(subj_lc)
    if fm:
        tok = fm.group(1)
        if tok in origin: score += 5
        elif tok in dest: score -= 5
    if tm:
        tok = tm.group(1)
        if tok in dest: score += 5
        elif tok in origin: score -= 5
    return score


def score_match(thread_subject: str, purchase_text: str, line_desc: str = "") -> int:
    """Token overlap + directional bonus."""
    a = subject_tokens(thread_subject)
    b = subject_tokens(purchase_text)
    base = len(a & b)
    dir_bonus = directional_score(thread_subject, line_desc) if line_desc else 0
    return base + dir_bonus


def match_threads_to_purchases(
    threads: list[dict],
    purchases: list[dict],
    *,
    customer: str = "",
    min_score: int = 2,
) -> dict[str, list[dict]]:
    """For each Purchase Id, return the list of threads whose subject best
    matches its line description + memo.

    Disambiguation strategy:
      1. If `customer` is set, the thread subject MUST
         contain the customer name token. This kills false positives from
         other trips that happen to share generic words like "taxi" or "hotel".
      2. Among trip-tagged threads, score by token overlap with the
         Purchase's vendor + line description + memo. Require score >=
         min_score (default 2) so a single shared token doesn't trigger
         a match.
      3. Each thread is assigned to its single best-scoring Purchase.
    """
    out: dict[str, list[dict]] = {}
    purchase_blobs: dict[str, str] = {}
    purchase_line_desc: dict[str, str] = {}
    for p in purchases:
        line_desc = ((p.get("Line") or [{}])[0]).get("Description") or ""
        memo = p.get("PrivateNote") or ""
        vendor = (p.get("EntityRef") or {}).get("name", "") or ""
        purchase_blobs[p["Id"]] = f"{vendor} {line_desc} {memo}"
        purchase_line_desc[p["Id"]] = line_desc

    customer_lc = (customer or "").lower().strip()

    for t in threads:
        subj = t.get("subject") or ""
        subj_lc = subj.lower()
        # Gate (1): customer-name filter
        if customer_lc and customer_lc not in subj_lc:
            continue
        # Gate (2): token-overlap score (with directional bonus)
        best_pid, best_score = None, 0
        for pid, blob in purchase_blobs.items():
            sc = score_match(subj, blob, line_desc=purchase_line_desc[pid])
            if sc > best_score:
                best_pid, best_score = pid, sc
        if best_pid and best_score >= min_score:
            out.setdefault(best_pid, []).append({**t, "_score": best_score})
    for pid in out:
        out[pid].sort(key=lambda x: -x["_score"])
    return out
