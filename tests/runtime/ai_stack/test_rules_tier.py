"""Tests for RulesTier."""

from __future__ import annotations

from app.runtime.ai_stack.tier import TierKind
from app.runtime.ai_stack.tiers import RulesTier


def _newsletter_rule(input_data: dict) -> tuple[dict | None, float]:
    body = input_data.get("body", "").lower()
    if "unsubscribe" in body:
        return ({"label": "newsletters"}, 0.97)
    return (None, 0.0)


def test_rules_tier_resolves_on_match() -> None:
    tier = RulesTier(tier_id="rules", rule_fn=_newsletter_rule)
    pred = tier.predict({"body": "Click here to unsubscribe."})
    assert pred.tier_id == "rules"
    assert pred.output == {"label": "newsletters"}
    assert pred.confidence == 0.97
    assert pred.abstained is False


def test_rules_tier_abstains_on_no_match() -> None:
    tier = RulesTier(tier_id="rules", rule_fn=_newsletter_rule)
    pred = tier.predict({"body": "Hi, can we meet tomorrow?"})
    assert pred.abstained is True
    assert pred.confidence == 0.0
    assert pred.output == {}


def test_rules_tier_kind() -> None:
    tier = RulesTier(tier_id="rules", rule_fn=_newsletter_rule)
    assert tier.tier_kind == TierKind.RULES


def test_rules_tier_default_threshold() -> None:
    tier = RulesTier(tier_id="rules", rule_fn=_newsletter_rule)
    assert tier.confidence_threshold == 0.85


def test_rules_tier_records_latency() -> None:
    tier = RulesTier(tier_id="rules", rule_fn=_newsletter_rule)
    pred = tier.predict({"body": "unsubscribe"})
    assert pred.latency_ms >= 0
