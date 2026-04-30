"""Tests for TieredTaskRunner — the cascade with early-stop + EvalRecord write."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.eval.record import Prediction, RealityStatus
from app.eval.storage import EvalRecordStore
from app.runtime.ai_stack.runner import CascadeAbstainedError, TieredTaskRunner
from app.runtime.ai_stack.task import (
    EscalationPolicy,
    GraduationExperimentConfig,
    Task,
    TaskConfig,
)
from app.runtime.ai_stack.tier import TierKind
from app.runtime.ai_stack.tiers import HumanTier


class _ScriptedTier:
    """Tier that returns a canned Prediction. Use in tests instead of real tiers."""

    def __init__(
        self,
        *,
        tier_id: str,
        tier_kind: TierKind,
        prediction: Prediction,
        confidence_threshold: float = 0.85,
    ) -> None:
        self.tier_id = tier_id
        self.tier_kind = tier_kind
        self.prediction = prediction
        self.confidence_threshold = confidence_threshold
        self.calls = 0

    def predict(self, _input_data: dict[str, Any]) -> Prediction:
        self.calls += 1
        return self.prediction


class _StubAskPoster:
    def __init__(self, ask_id: str = "ask-001") -> None:
        self.ask_id = ask_id
        self.calls = 0

    def post_ask(self, **_kwargs: Any) -> str:
        self.calls += 1
        return self.ask_id


def _confident(tier_id: str, output: dict | None = None) -> Prediction:
    return Prediction(
        tier_id=tier_id,
        output=output or {"label": "newsletters"},
        confidence=0.95,
        abstained=False,
    )


def _abstaining(tier_id: str) -> Prediction:
    return Prediction(
        tier_id=tier_id,
        output={},
        confidence=0.0,
        abstained=True,
    )


def _make_config(
    *,
    active_tier_id: str = "cloud_llm",
    escalation_policy: EscalationPolicy = EscalationPolicy.ASK_HUMAN,
    graduation_experiment: GraduationExperimentConfig | None = None,
) -> TaskConfig:
    return TaskConfig(
        task_id="t",
        description="test",
        input_schema_class="x",
        output_schema_class="x",
        active_tier_id=active_tier_id,
        escalation_policy=escalation_policy,
        graduation_experiment=graduation_experiment,
    )


def _runner(tmp_path: Path) -> TieredTaskRunner:
    return TieredTaskRunner(
        eval_store=EvalRecordStore(root=tmp_path / "eval"),
        clock=lambda: datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC),
    )


def test_cascade_stops_at_first_resolved_tier(tmp_path: Path) -> None:
    rules = _ScriptedTier(
        tier_id="rules", tier_kind=TierKind.RULES, prediction=_confident("rules")
    )
    cloud = _ScriptedTier(
        tier_id="cloud_llm",
        tier_kind=TierKind.CLOUD_LLM,
        prediction=_confident("cloud_llm"),
    )
    task = Task(config=_make_config(), tiers=[rules, cloud])
    record = _runner(tmp_path).run(task, input_id="msg-1", input_data={"x": 1})
    assert record.escalation_chain == ["rules"]
    assert "rules" in record.tier_predictions
    assert "cloud_llm" not in record.tier_predictions
    assert record.active_decision == {"label": "newsletters"}
    assert rules.calls == 1
    assert cloud.calls == 0


def test_cascade_escalates_when_cheap_tier_abstains(tmp_path: Path) -> None:
    rules = _ScriptedTier(
        tier_id="rules", tier_kind=TierKind.RULES, prediction=_abstaining("rules")
    )
    cloud = _ScriptedTier(
        tier_id="cloud_llm",
        tier_kind=TierKind.CLOUD_LLM,
        prediction=_confident("cloud_llm", output={"label": "personal"}),
    )
    task = Task(config=_make_config(), tiers=[rules, cloud])
    record = _runner(tmp_path).run(task, input_id="msg-2", input_data={"x": 2})
    assert record.escalation_chain == ["rules", "cloud_llm"]
    assert record.active_decision == {"label": "personal"}


def test_full_abstain_with_ask_human_invokes_human_tier(tmp_path: Path) -> None:
    rules = _ScriptedTier(
        tier_id="rules", tier_kind=TierKind.RULES, prediction=_abstaining("rules")
    )
    cloud = _ScriptedTier(
        tier_id="cloud_llm",
        tier_kind=TierKind.CLOUD_LLM,
        prediction=_abstaining("cloud_llm"),
    )
    poster = _StubAskPoster(ask_id="ask-77")
    human = HumanTier(tier_id="human", ask_poster=poster, task_id="t")
    task = Task(config=_make_config(), tiers=[rules, cloud, human])
    record = _runner(tmp_path).run(task, input_id="msg-3", input_data={"x": 3})
    assert record.escalation_chain == ["rules", "cloud_llm", "human"]
    assert record.ask_id == "ask-77"
    assert poster.calls == 1


def test_use_active_policy_returns_active_output_on_full_abstain(
    tmp_path: Path,
) -> None:
    rules = _ScriptedTier(
        tier_id="rules", tier_kind=TierKind.RULES, prediction=_abstaining("rules")
    )
    abstaining_with_output = Prediction(
        tier_id="cloud_llm",
        output={"label": "best_guess"},
        confidence=0.6,
        abstained=True,
    )
    cloud = _ScriptedTier(
        tier_id="cloud_llm",
        tier_kind=TierKind.CLOUD_LLM,
        prediction=abstaining_with_output,
    )
    task = Task(
        config=_make_config(escalation_policy=EscalationPolicy.USE_ACTIVE),
        tiers=[rules, cloud],
    )
    record = _runner(tmp_path).run(task, input_id="msg-4", input_data={"x": 4})
    assert record.active_decision == {"label": "best_guess"}
    assert record.ask_id is None


def test_drop_policy_raises_on_full_abstain(tmp_path: Path) -> None:
    rules = _ScriptedTier(
        tier_id="rules", tier_kind=TierKind.RULES, prediction=_abstaining("rules")
    )
    cloud = _ScriptedTier(
        tier_id="cloud_llm",
        tier_kind=TierKind.CLOUD_LLM,
        prediction=_abstaining("cloud_llm"),
    )
    task = Task(
        config=_make_config(escalation_policy=EscalationPolicy.DROP),
        tiers=[rules, cloud],
    )
    with pytest.raises(CascadeAbstainedError):
        _runner(tmp_path).run(task, input_id="msg-5", input_data={"x": 5})


def test_eval_record_includes_window_end_and_pending_status(tmp_path: Path) -> None:
    rules = _ScriptedTier(
        tier_id="rules", tier_kind=TierKind.RULES, prediction=_confident("rules")
    )
    task = Task(config=_make_config(active_tier_id="rules"), tiers=[rules])
    record = _runner(tmp_path).run(task, input_id="msg-6", input_data={"x": 6})
    assert record.reality_status == RealityStatus.PENDING
    assert record.is_ground_truth is False
    expected_window_end = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)  # +7 days
    assert record.reality_observation_window_ends_at == expected_window_end


def test_runner_appends_record_to_store(tmp_path: Path) -> None:
    rules = _ScriptedTier(
        tier_id="rules", tier_kind=TierKind.RULES, prediction=_confident("rules")
    )
    task = Task(config=_make_config(active_tier_id="rules"), tiers=[rules])
    runner = _runner(tmp_path)
    runner.run(task, input_id="msg-a", input_data={"x": 1})
    runner.run(task, input_id="msg-b", input_data={"x": 2})
    records = runner.eval_store.read_all("t")
    assert len(records) == 2
    assert {r.input_id for r in records} == {"msg-a", "msg-b"}


def test_graduation_experiment_runs_candidate_when_sampled(tmp_path: Path) -> None:
    """Active=rules; candidate=local_llm (a tier ABOVE active). Shadow runs
    the candidate on inputs even though the cascade stopped at rules."""

    rules = _ScriptedTier(
        tier_id="rules", tier_kind=TierKind.RULES, prediction=_confident("rules")
    )
    candidate = _ScriptedTier(
        tier_id="local_llm",
        tier_kind=TierKind.LOCAL_LLM,
        prediction=_confident("local_llm", output={"label": "candidate_says"}),
    )
    task = Task(
        config=_make_config(
            active_tier_id="rules",
            graduation_experiment=GraduationExperimentConfig(
                candidate_tier_id="local_llm",
                sample_rate=0.5,
            ),
        ),
        tiers=[rules, candidate],
    )
    runner = TieredTaskRunner(
        eval_store=EvalRecordStore(root=tmp_path / "eval"),
        sampler=lambda: 0.1,  # below 0.5 → sampled in
    )
    record = runner.run(task, input_id="msg-7", input_data={"x": 7})
    # Candidate ran (shadow), but rules resolved the cascade.
    assert "local_llm" in record.tier_predictions
    assert record.active_decision == {"label": "newsletters"}
    assert record.escalation_chain == ["rules"]  # candidate doesn't enter chain
    assert candidate.calls == 1


def test_graduation_experiment_skips_candidate_when_not_sampled(
    tmp_path: Path,
) -> None:
    rules = _ScriptedTier(
        tier_id="rules", tier_kind=TierKind.RULES, prediction=_confident("rules")
    )
    candidate = _ScriptedTier(
        tier_id="local_llm",
        tier_kind=TierKind.LOCAL_LLM,
        prediction=_confident("local_llm"),
    )
    task = Task(
        config=_make_config(
            active_tier_id="rules",
            graduation_experiment=GraduationExperimentConfig(
                candidate_tier_id="local_llm",
                sample_rate=0.5,
            ),
        ),
        tiers=[rules, candidate],
    )
    runner = TieredTaskRunner(
        eval_store=EvalRecordStore(root=tmp_path / "eval"),
        sampler=lambda: 0.9,  # above 0.5 → sampled out
    )
    record = runner.run(task, input_id="msg-8", input_data={"x": 8})
    assert "local_llm" not in record.tier_predictions
    assert candidate.calls == 0
