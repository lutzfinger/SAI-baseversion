"""
cleanup — apply (propose) operator bookkeeping rules across recent Purchases.

After each customer invoice is sent, the operator may leave persistent
accounting rules in their overlay's `bookkeeping-rules.md`, e.g.:

  Rule R1 — Food while traveling
    Rule:      All food consumed while traveling for business is NOT
               reimbursable but IS a deductible business-travel cost.
    QB action: tag any food/restaurant expense to account
               Travel:Travel meals (Id=1150040006).
    Triggers:  Purchase whose vendor matches restaurant/cafe/bistro/bar
               OR existing AccountRef is meals category AND TxnDate is
               in an active trip window.

Per SAI principle #20 (Reflection may suggest, never auto-apply), this
module PROPOSES changes. The operator's two-phase commit (touch a
sentinel file OR run `apply-rule --rule R1 --confirm`) is required
before any QB write happens.

Public API:
    parse_rules(text) -> list[Rule]
    propose(purchases, rules) -> list[Proposal]
    write_proposal_doc(proposals, path)  # markdown the operator reads

Rule shape (deterministic — no LLM):
    rule_id   "R1"
    title     "Food while traveling"
    rule_text the stated rule
    qb_action what to change
    trigger_keywords  list[str] derived from the Triggers line
    trigger_regex     compiled regex of those keywords (case-insensitive)

A Proposal pairs one Purchase to the rule whose trigger keywords appear
in the Purchase's description / vendor / account name. Confidence is
"high" when 2+ keywords match, "medium" for 1, "low" if only the date
window matched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional


@dataclass
class Rule:
    rule_id: str
    title: str
    rule_text: str
    qb_action: str
    trigger_keywords: list[str]
    trigger_regex: re.Pattern


@dataclass
class Proposal:
    rule: Rule
    purchase: dict
    confidence: str       # "high" | "medium" | "low"
    matched_terms: list[str] = field(default_factory=list)
    reason: str = ""


# Words to STRIP from the "Triggers" line before extracting keywords.
# These are connectives / category labels, not actual signals.
_STOPWORDS = {
    "purchase", "purchases", "txndate", "accountref", "active",
    "trip", "trips", "window", "within", "during", "is", "in",
    "or", "and", "with", "that", "the", "an", "a", "of", "this",
    "where", "whose", "matches", "match", "category", "categories",
    "vendor", "vendors", "name", "names", "existing", "after",
    "before", "if", "to", "for", "from", "be", "are", "been",
    "city", "region", "outside", "side", "leg", "personal",
    "business", "customer's", "customers", "customer",
    # Common verbs that aren't signals:
    "ends", "occurs", "happens",
    # Punctuation cleanup
    "",
}


def parse_rules(text: str) -> list[Rule]:
    """Parse bookkeeping-rules.md into structured Rule objects.

    Recognised format (matches the operator's existing file):

        ## Rule R1 — Title
        - **Stated**: ...
        - **Rule**: ...
        - **QB action**: ...
        - **Triggers**: ...

    Headers must start with "## Rule" (case-sensitive) so the parser
    ignores narrative sections.
    """
    rules: list[Rule] = []
    # Split on every '## ' header — keep only blocks beginning with
    # '## Rule R<n>'. This bounds each rule's text so trailing
    # narrative sections (like "Cleanup pass (TBD)") don't leak into
    # the last rule's Triggers field.
    blocks = re.split(r"\n(?=##\s)", text)
    for blk in blocks:
        blk = blk.strip()
        if not blk.startswith("## Rule"):
            continue
        m = re.match(r"##\s*Rule\s+(R\d+)\s*[—\-:]?\s*(.+)", blk.splitlines()[0])
        if not m:
            continue
        rule_id, title = m.group(1), m.group(2).strip()

        def _field(label: str) -> str:
            mm = re.search(
                rf"-\s+\*\*{re.escape(label)}\*\*:\s*(.+?)(?=\n-\s+\*\*|\Z)",
                blk,
                re.DOTALL,
            )
            return mm.group(1).strip() if mm else ""

        rule_text = _field("Rule")
        qb_action = _field("QB action")
        triggers = _field("Triggers")
        keywords = _extract_keywords(triggers)
        regex = _compile_keyword_regex(keywords)
        rules.append(Rule(
            rule_id=rule_id,
            title=title,
            rule_text=rule_text,
            qb_action=qb_action,
            trigger_keywords=keywords,
            trigger_regex=regex,
        ))
    return rules


def _extract_keywords(triggers_text: str) -> list[str]:
    """Pull meaningful nouns out of a Triggers description.

    Multi-word noun phrases like "restaurant/cafe/bistro/bar" get split
    on "/" so each alternative is its own keyword. Hyphenated terms
    like "ground-transport" stay intact (they appear as single units
    in QB descriptions).
    """
    # Strip markdown emphasis
    t = re.sub(r"[*_`]+", " ", triggers_text or "")
    # Replace slashes with spaces so /-separated alternatives become
    # individual words.
    t = t.replace("/", " ")
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]+", t.lower())
    seen: list[str] = []
    seen_set: set[str] = set()
    for w in words:
        if len(w) < 3:
            continue
        if w in _STOPWORDS:
            continue
        if w in seen_set:
            continue
        seen_set.add(w)
        seen.append(w)
    return seen


def _compile_keyword_regex(keywords: list[str]) -> re.Pattern:
    if not keywords:
        return re.compile(r"^$")  # never matches
    joined = "|".join(re.escape(k) for k in keywords)
    return re.compile(rf"\b({joined})\b", re.IGNORECASE)


def _purchase_text(p: dict) -> str:
    """Concatenate the searchable fields of a QB Purchase."""
    parts: list[str] = []
    if (p.get("EntityRef") or {}).get("name"):
        parts.append(p["EntityRef"]["name"])
    if p.get("PrivateNote"):
        parts.append(p["PrivateNote"])
    for ln in p.get("Line", []) or []:
        if ln.get("Description"):
            parts.append(ln["Description"])
        det = ln.get("AccountBasedExpenseLineDetail") or {}
        if det.get("AccountRef", {}).get("name"):
            parts.append(det["AccountRef"]["name"])
    return " | ".join(parts)


def propose(
    purchases: list[dict],
    rules: list[Rule],
    trip_start: Optional[date] = None,
    trip_end: Optional[date] = None,
) -> list[Proposal]:
    """For each purchase × rule, emit a proposal if the rule's triggers fire.

    Date-window check: if `trip_start`/`trip_end` are passed AND the rule
    mentions "trip window", the purchase's TxnDate must be inside that
    window. Otherwise, all dates qualify.
    """
    out: list[Proposal] = []
    for p in purchases:
        text = _purchase_text(p)
        for r in rules:
            matched = r.trigger_regex.findall(text) if r.trigger_keywords else []
            unique_matches = sorted({m.lower() for m in matched})
            if not unique_matches:
                continue
            confidence = "high" if len(unique_matches) >= 2 else "medium"
            # If the rule's trigger text mentions 'trip window', enforce it.
            if "trip window" in " ".join(r.trigger_keywords).lower():
                pdate_raw = p.get("TxnDate")
                pdate = None
                if pdate_raw:
                    try:
                        pdate = date.fromisoformat(pdate_raw)
                    except ValueError:
                        pdate = None
                if trip_start and trip_end and pdate:
                    if not (trip_start <= pdate <= trip_end):
                        confidence = "low"
            out.append(Proposal(
                rule=r,
                purchase=p,
                confidence=confidence,
                matched_terms=unique_matches,
                reason=f"matched {len(unique_matches)} keyword(s): {', '.join(unique_matches)}",
            ))
    return out


def write_proposal_doc(proposals: list[Proposal], path: Path) -> None:
    """Render proposals as a markdown doc the operator reviews."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Cleanup-pass proposals")
    lines.append("")
    lines.append(
        f"Generated by `cleanup-pass`. Each section lists Purchases that "
        f"matched a rule's trigger keywords. Per SAI principle #20, **nothing "
        f"is auto-applied** — review each proposal and apply manually in QB, "
        f"or run `apply-rule --rule <id> --confirm` (TBD)."
    )
    lines.append("")
    by_rule: dict[str, list[Proposal]] = {}
    for pr in proposals:
        by_rule.setdefault(pr.rule.rule_id, []).append(pr)
    for rule_id, items in by_rule.items():
        r = items[0].rule
        lines.append(f"## Rule {rule_id} — {r.title}")
        lines.append("")
        lines.append(f"**Rule text:** {r.rule_text}")
        lines.append("")
        lines.append(f"**Proposed QB action:** {r.qb_action}")
        lines.append("")
        lines.append(f"**Trigger keywords:** {', '.join(r.trigger_keywords)}")
        lines.append("")
        lines.append(f"### {len(items)} candidate(s)")
        lines.append("")
        lines.append("| Confidence | Purchase Id | Date | Amount | Vendor | Matched terms |")
        lines.append("|---|---|---|---|---|---|")
        for pr in items:
            p = pr.purchase
            vendor = (p.get("EntityRef") or {}).get("name", "?")
            lines.append(
                f"| {pr.confidence} "
                f"| {p.get('Id', '?')} "
                f"| {p.get('TxnDate', '?')} "
                f"| {p.get('TotalAmt', '?')} "
                f"| {vendor} "
                f"| {', '.join(pr.matched_terms)} |"
            )
        lines.append("")
    path.write_text("\n".join(lines))
