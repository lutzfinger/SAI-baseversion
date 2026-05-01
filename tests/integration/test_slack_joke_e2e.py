"""End-to-end integration: slack_joke task shape through the AI Stack.

Verifies the slack_joke cascade SHAPE composes correctly using only
public framework + public schemas. The actual canned-joke rule logic
and prompt template ship in the operator's private overlay (see
`app/tasks/slack_joke.py` in private SAI for the live wiring).

Three contracts are tested:

  - Topic-keyword request → rules tier resolves; cloud is never called.
  - Novel request → rules abstains → cloud runs; cloud's output wins.
  - Cloud abstain → USE_ACTIVE returns active tier's empty output. The
    "always-post-something" fallback is the CALLER's responsibility
    (the Slack DM dispatcher detects empty active_decision and posts a
    deterministic canned joke). Documented here so the contract is
    visible.

The cloud tier is scripted (StubCloudTier) so no OpenAI key is needed.
The rules tier is also scripted, mirroring the canned-keyword shape
of the private factory.
"""

from __future__ import annotations

from typing import Any

from app.eval.record import Prediction
from app.eval.storage import EvalRecordStore
from app.runtime.ai_stack import (
    RulesTier,
    Task,
    TaskConfig,
    TieredTaskRunner,
    Tier,
)
from app.runtime.ai_stack.tier import TierKind
from app.runtime.ai_stack.task import EscalationPolicy

# Mirrors the SafeJokeResponse shape (public schema in slack_joke_io.py).
_SAMPLE_CANNED = {
    "request_summary": "joke about meetings",
    "joke_text": "Why did the calendar stay calm? It already blocked.",
    "safe_for_work": True,
    "content_rating": "g",
    "confidence": 0.95,
}

def _build_rules_tier(*, threshold: float = 0.85) -> RulesTier:
    """Topic-keyword rules tier — resolves on match, abstains otherwise."""

    def rule_fn(input_data: dict[str, Any]) -> tuple[dict[str, Any] | None, float]:
        text = str(input_data.get("request_text") or "").lower().strip()
        if not text:
            return None, 0.0
        if "meeting" in text or "calendar" in text:
            return dict(_SAMPLE_CANNED), 0.95
        return None, 0.0  # no keyword match — let cloud try

    return RulesTier(
        tier_id="rules",
        rule_fn=rule_fn,
        confidence_threshold=threshold,
    )


class _StubCloudTier:
    """Records calls; returns a scripted Prediction or abstains."""

    tier_id = "cloud_llm"
    tier_kind = TierKind.CLOUD_LLM
    confidence_threshold = 0.6

    def __init__(self, *, return_prediction: Prediction | None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._return = return_prediction

    def predict(self, input_data: dict[str, Any]) -> Prediction:
        self.calls.append(input_data)
        if self._return is None:
            return Prediction(
                tier_id=self.tier_id,
                output={},
                confidence=0.0,
                abstained=True,
                reasoning="stub: abstain",
            )
        return self._return


def _make_task(*, rules_tier: Tier, cloud_tier: Tier) -> Task:
    config = TaskConfig(
        task_id="slack_joke",
        description="test",
        input_schema_class="app.tasks.slack_joke_io.JokeRequest",
        output_schema_class="app.tasks.slack_joke_io.JokeResponse",
        active_tier_id="cloud_llm",
        escalation_policy=EscalationPolicy.USE_ACTIVE,
        reality_observation_window_days=7,
        graduation_thresholds={},
    )
    return Task(config=config, tiers=[rules_tier, cloud_tier])


def test_topic_keyword_short_circuits_at_rules(tmp_path) -> None:
    """When request matches a topic keyword, rules resolves at 0.95 and
    cloud is never called."""

    rules_tier = _build_rules_tier()
    stub_cloud = _StubCloudTier(return_prediction=None)
    task = _make_task(rules_tier=rules_tier, cloud_tier=stub_cloud)

    runner = TieredTaskRunner(eval_store=EvalRecordStore(root=tmp_path))
    record = runner.run(
        task,
        input_id="dm-001",
        input_data={
            "request_text": "tell me a joke about meetings",
            "reply_channel": "D123",
        },
    )

    assert record.escalation_chain == ["rules"]
    assert stub_cloud.calls == [], "cloud must not be called on keyword match"
    decision = record.active_decision or {}
    assert decision.get("safe_for_work") is True
    assert "calendar" in decision.get("joke_text", "").lower()


def test_novel_request_falls_through_to_cloud(tmp_path) -> None:
    """Novel request → rules low-confidence → cloud runs and supersedes."""

    rules_tier = _build_rules_tier()
    cloud_joke = {
        "request_summary": "joke about purple kangaroos",
        "joke_text": "Why did the purple kangaroo open a bakery? It loved jumping into bread.",
        "safe_for_work": True,
        "content_rating": "g",
        "confidence": 0.9,
    }
    stub_cloud = _StubCloudTier(
        return_prediction=Prediction(
            tier_id="cloud_llm",
            output=cloud_joke,
            confidence=0.9,
            abstained=False,
        )
    )
    task = _make_task(rules_tier=rules_tier, cloud_tier=stub_cloud)

    runner = TieredTaskRunner(eval_store=EvalRecordStore(root=tmp_path))
    record = runner.run(
        task,
        input_id="dm-002",
        input_data={
            "request_text": "tell me a joke about purple kangaroos",
            "reply_channel": "D123",
        },
    )

    assert "rules" in record.escalation_chain
    assert "cloud_llm" in record.escalation_chain
    assert len(stub_cloud.calls) == 1
    assert (record.active_decision or {}).get("joke_text", "").startswith(
        "Why did the purple kangaroo"
    )


def test_cloud_abstains_use_active_returns_empty_active_decision(tmp_path) -> None:
    """USE_ACTIVE: when cloud (the active tier) abstains, active_decision
    is the cloud tier's empty output. The CALLER is responsible for
    detecting `active_decision == {}` and posting a canned fallback;
    the Task itself doesn't fabricate one.

    This is the documented contract — the runner doesn't reach back to
    a lower tier for safety-net output. Tasks that need always-resolve
    behaviour put that responsibility in their dispatcher."""

    rules_tier = _build_rules_tier()
    stub_cloud = _StubCloudTier(return_prediction=None)  # abstains
    task = _make_task(rules_tier=rules_tier, cloud_tier=stub_cloud)

    runner = TieredTaskRunner(eval_store=EvalRecordStore(root=tmp_path))
    record = runner.run(
        task,
        input_id="dm-003",
        input_data={
            "request_text": "tell me a joke about purple kangaroos",
            "reply_channel": "D123",
        },
    )

    assert "rules" in record.escalation_chain
    assert "cloud_llm" in record.escalation_chain
    # Active decision is empty — caller checks this and uses canned_fallback_joke().
    assert record.active_decision == {}
    # Both tiers' predictions are recorded; rules abstained, cloud abstained.
    assert record.tier_predictions["rules"].abstained
    assert record.tier_predictions["cloud_llm"].abstained


def test_empty_request_abstains_at_rules(tmp_path) -> None:
    """Empty request_text → rules returns (None, 0.0) → cascade moves on."""

    rules_tier = _build_rules_tier()
    stub_cloud = _StubCloudTier(return_prediction=None)
    task = _make_task(rules_tier=rules_tier, cloud_tier=stub_cloud)

    runner = TieredTaskRunner(eval_store=EvalRecordStore(root=tmp_path))
    record = runner.run(
        task,
        input_id="dm-004",
        input_data={"request_text": "", "reply_channel": "D123"},
    )

    rules_pred = record.tier_predictions["rules"]
    assert rules_pred.abstained
