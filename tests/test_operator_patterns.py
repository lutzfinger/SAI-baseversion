"""Pattern-bound parser tests for #sai-eval operator commands.

Per PRINCIPLES.md §16b — these parsers are the "pattern-bound" gate from
the five gates of #9. They must:

  - Recognise the exact registered patterns
  - Reject everything else (no false positives → no surprise edits)
  - Surface clear errors on partial match (operator typo'd; we want a
    helpful Slack reply, not silent ignore)
"""

from __future__ import annotations

import pytest

from app.eval.operator_patterns import (
    AddEvalProposal,
    AddRuleProposal,
    ParseError,
    parse_add_eval,
    parse_add_rule,
)

OP = "U_OPERATOR_TEST"


# ─── add rule ──────────────────────────────────────────────────────────


class TestParseAddRule:
    def test_canonical_form(self) -> None:
        p = parse_add_rule(
            "add rule: from acme@example.com → L1/customers", proposed_by=OP
        )
        assert isinstance(p, AddRuleProposal)
        assert p.target == "acme@example.com"
        assert p.target_kind == "sender_email"
        assert p.expected_level1_classification == "customers"
        assert p.proposed_by == OP

    def test_domain_form(self) -> None:
        p = parse_add_rule(
            "add rule: from example.com → L1/customers", proposed_by=OP
        )
        assert p is not None
        assert p.target == "example.com"
        assert p.target_kind == "sender_domain"

    def test_no_from_keyword(self) -> None:
        p = parse_add_rule(
            "add rule: acme@example.com → customers", proposed_by=OP
        )
        assert p is not None
        assert p.target == "acme@example.com"
        assert p.expected_level1_classification == "customers"

    def test_ascii_arrow(self) -> None:
        p = parse_add_rule(
            "add rule: from acme@example.com -> customers", proposed_by=OP
        )
        assert p is not None and p.expected_level1_classification == "customers"

    def test_to_keyword_as_arrow(self) -> None:
        p = parse_add_rule(
            "add rule acme@example.com to customers", proposed_by=OP
        )
        assert p is not None and p.target == "acme@example.com"

    def test_display_name_bucket(self) -> None:
        # "Customers" → "customers"  (display-name → bucket)
        p = parse_add_rule(
            "add rule: from acme@example.com → Customers", proposed_by=OP
        )
        assert p is not None and p.expected_level1_classification == "customers"

    def test_singular_bucket(self) -> None:
        # "customer" → "customers"
        p = parse_add_rule(
            "add rule: from acme.com → customer", proposed_by=OP
        )
        assert p is not None and p.expected_level1_classification == "customers"

    def test_target_lowercased(self) -> None:
        p = parse_add_rule(
            "add rule: from ACME@Example.COM → customers", proposed_by=OP
        )
        assert p is not None and p.target == "acme@example.com"

    def test_returns_none_on_unrelated_text(self) -> None:
        # Operator just chatting; bot must NOT silently treat this as a rule.
        for text in [
            "hey, can you check the cron?",
            "this email was tagged wrong",
            "👍",
            "",
        ]:
            assert parse_add_rule(text, proposed_by=OP) is None

    def test_raises_on_partial_match_bad_bucket(self) -> None:
        # Operator clearly meant "add rule" but typo'd the bucket.
        with pytest.raises(ParseError, match="isn't one of my labels"):
            parse_add_rule(
                "add rule: from acme@example.com → cheri", proposed_by=OP
            )

    def test_raises_on_partial_match_bad_grammar(self) -> None:
        # Operator said "add rule" but the rest is garbage.
        with pytest.raises(ParseError, match="couldn't parse"):
            parse_add_rule("add rule please halp", proposed_by=OP)
        # The error message format itself
        with pytest.raises(ParseError, match="add rule:"):
            parse_add_rule("add rule please halp", proposed_by=OP)


# ─── add eval ──────────────────────────────────────────────────────────


class TestParseAddEval:
    def test_canonical_form(self) -> None:
        p = parse_add_eval(
            "msg-12345 should have been L1/customers", proposed_by=OP
        )
        assert isinstance(p, AddEvalProposal)
        assert p.message_ref == "msg-12345"
        assert p.expected_level1_classification == "customers"

    def test_with_gmail_url(self) -> None:
        p = parse_add_eval(
            "https://mail.google.com/mail/u/0/#inbox/abc123 should have been L1/finance",
            proposed_by=OP,
        )
        assert p is not None
        assert "mail.google.com" in p.message_ref
        assert p.expected_level1_classification == "finance"

    def test_should_be_form(self) -> None:
        # "should be" is also accepted ("should have been" is canonical)
        p = parse_add_eval("msg-1 should be customers", proposed_by=OP)
        assert p is not None and p.expected_level1_classification == "customers"

    def test_bucket_with_l1_prefix(self) -> None:
        p = parse_add_eval(
            "msg-1 should have been L1/customers", proposed_by=OP
        )
        assert p is not None and p.expected_level1_classification == "customers"

    def test_returns_none_on_unrelated_text(self) -> None:
        for text in [
            "should we discuss this tomorrow?",  # has "should" but doesn't match grammar
            "looks fine",
            "",
        ]:
            assert parse_add_eval(text, proposed_by=OP) is None

    def test_raises_on_bad_bucket(self) -> None:
        with pytest.raises(ParseError, match="isn't one of my labels"):
            parse_add_eval(
                "msg-1 should have been wibblewobble", proposed_by=OP
            )


# ─── round-trip: parsers don't false-match each other ─────────────────


def test_parsers_disjoint() -> None:
    """An add_rule string must NOT also match add_eval (and vice versa).
    Otherwise the slack_bot would have to disambiguate by trying both,
    which invites surprises."""
    rule_text = "add rule: from acme@example.com → customers"
    eval_text = "msg-1 should have been customers"

    assert parse_add_rule(rule_text, proposed_by=OP) is not None
    assert parse_add_eval(rule_text, proposed_by=OP) is None

    assert parse_add_rule(eval_text, proposed_by=OP) is None
    assert parse_add_eval(eval_text, proposed_by=OP) is not None
