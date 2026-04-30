"""Tests for ClassifierTier."""

from __future__ import annotations

from app.runtime.ai_stack.tier import TierKind
from app.runtime.ai_stack.tiers import ClassifierTier


def _confident_classifier(input_data: dict) -> tuple[dict, float]:
    return ({"label": input_data.get("category", "other")}, 0.92)


def _low_confidence_classifier(_input_data: dict) -> tuple[dict, float]:
    return ({"label": "personal"}, 0.45)


def _broken_classifier(_input_data: dict) -> tuple[dict, float]:
    raise RuntimeError("model file corrupt")


def test_classifier_tier_resolves_above_threshold() -> None:
    tier = ClassifierTier(tier_id="cls", classify_fn=_confident_classifier)
    pred = tier.predict({"category": "customers"})
    assert pred.abstained is False
    assert pred.output == {"label": "customers"}
    assert pred.confidence == 0.92


def test_classifier_tier_abstains_below_threshold() -> None:
    tier = ClassifierTier(
        tier_id="cls", classify_fn=_low_confidence_classifier, confidence_threshold=0.85
    )
    pred = tier.predict({})
    assert pred.abstained is True
    assert pred.confidence == 0.45
    # Low-confidence output is still surfaced (audit) but tier abstained.
    assert pred.output == {"label": "personal"}


def test_classifier_tier_abstains_on_exception() -> None:
    tier = ClassifierTier(tier_id="cls", classify_fn=_broken_classifier)
    pred = tier.predict({})
    assert pred.abstained is True
    assert pred.confidence == 0.0
    assert "RuntimeError" in (pred.reasoning or "")


def test_classifier_tier_kind() -> None:
    tier = ClassifierTier(tier_id="cls", classify_fn=_confident_classifier)
    assert tier.tier_kind == TierKind.CLASSIFIER
