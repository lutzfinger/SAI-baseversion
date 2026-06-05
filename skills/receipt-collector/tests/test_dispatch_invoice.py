"""INVOICE_DRAFT dispatch classification + no regression on COST_COMPILER / IGNORE."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib import dispatch_agent as D  # noqa: E402


def _rule(subject, body=""):
    return D._rules_classify(subject, body)


def test_invoice_draft_classifies():
    d = _rule("send an invoice to Gregory over 1500 for keynote")
    assert d is not None and d.verdict is D.Verdict.INVOICE_DRAFT
    d2 = _rule("send Gregory an invoice over $1,500")
    assert d2 is not None and d2.verdict is D.Verdict.INVOICE_DRAFT


def test_cost_compiler_still_wins_for_trips():
    # trip/receipts/expenses context must stay COST_COMPILER, never INVOICE_DRAFT
    d = _rule("compile my travel receipts for the Berlin trip")
    assert d is not None and d.verdict is D.Verdict.COST_COMPILER
    d2 = _rule("create an invoice for the customer trip to Berlin with receipts")
    assert d2 is not None and d2.verdict is D.Verdict.COST_COMPILER


def test_ignore_not_stolen():
    d = _rule("Your flight is now boarding for gate A12")
    assert d is not None and d.verdict is D.Verdict.IGNORE


def test_enum_and_case_map_and_prompt():
    assert "INVOICE_DRAFT" in {v.value for v in D.Verdict}
    assert D.Verdict.INVOICE_DRAFT in D.CASE_FOR_VERDICT
    assert "INVOICE_DRAFT" in D._LLM_SYSTEM_PROMPT
