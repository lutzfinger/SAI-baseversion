"""Tests for the Tier Protocol and shared helpers."""

from __future__ import annotations

import pytest

from app.eval.record import Prediction
from app.runtime.ai_stack.tier import (
    TIER_KIND_ORDER,
    Tier,
    TierKind,
    is_resolved,
)
from app.runtime.ai_stack.tiers import RulesTier


def test_tier_kind_ordering_is_cheapest_to_most_expensive() -> None:
    assert TIER_KIND_ORDER == (
        TierKind.RULES,
        TierKind.CLASSIFIER,
        TierKind.LOCAL_LLM,
        TierKind.CLOUD_LLM,
        TierKind.HUMAN,
    )


def test_rules_tier_satisfies_protocol() -> None:
    tier = RulesTier(
        tier_id="rules",
        rule_fn=lambda _: ({"label": "x"}, 0.95),
    )
    assert isinstance(tier, Tier)


def test_is_resolved_requires_non_abstain_and_threshold() -> None:
    above = Prediction(
        tier_id="x", output={"label": "y"}, confidence=0.9, abstained=False
    )
    below = Prediction(
        tier_id="x", output={"label": "y"}, confidence=0.5, abstained=False
    )
    abstained = Prediction(
        tier_id="x", output={}, confidence=0.0, abstained=True
    )
    assert is_resolved(above, threshold=0.85) is True
    assert is_resolved(below, threshold=0.85) is False
    assert is_resolved(abstained, threshold=0.0) is False


def test_tier_kind_str_values_match_yaml_keys() -> None:
    # Task config files use the string values; lock them down.
    assert TierKind.RULES.value == "rules"
    assert TierKind.CLOUD_LLM.value == "cloud_llm"
    assert TierKind.HUMAN.value == "human"


@pytest.mark.parametrize(
    "kind",
    list(TierKind),
)
def test_every_tier_kind_appears_in_order(kind: TierKind) -> None:
    assert kind in TIER_KIND_ORDER
