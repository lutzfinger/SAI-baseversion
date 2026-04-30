"""Tests for app.runtime.ai_stack.task."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.runtime.ai_stack.task import (
    EscalationPolicy,
    GraduationExperimentConfig,
    GraduationThresholds,
    Task,
    TaskConfig,
)
from app.runtime.ai_stack.tier import TierKind
from app.runtime.ai_stack.tiers import RulesTier


def _rule_fn(_input: dict) -> tuple[dict | None, float]:
    return ({"label": "x"}, 0.95)


def _make_tier(tier_id: str) -> RulesTier:
    return RulesTier(tier_id=tier_id, rule_fn=_rule_fn)


def _make_config(**overrides) -> TaskConfig:
    defaults = dict(
        task_id="email_classification",
        description="L1/L2 email classification",
        input_schema_class="app.workers.email_models.EmailMessage",
        output_schema_class="app.workers.email_models.EmailClassification",
        active_tier_id="cloud_llm",
    )
    defaults.update(overrides)
    return TaskConfig(**defaults)


def test_task_config_yaml_round_trip(tmp_path: Path) -> None:
    config = _make_config(
        graduation_thresholds={
            TierKind.RULES: GraduationThresholds(precision=0.95, recall=0.80),
            TierKind.LOCAL_LLM: GraduationThresholds(precision=0.85, recall=0.85),
        },
        graduation_experiment=GraduationExperimentConfig(
            candidate_tier_id="local_llm",
            sample_rate=0.20,
        ),
    )
    path = tmp_path / "task.yaml"
    path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    restored = TaskConfig.model_validate_json(path.read_text(encoding="utf-8"))
    assert restored == config
    assert restored.graduation_thresholds[TierKind.RULES].precision == 0.95


def test_task_config_default_window_is_seven_days() -> None:
    config = _make_config()
    assert config.reality_observation_window_days == 7
    assert config.escalation_policy == EscalationPolicy.ASK_HUMAN


def test_task_rejects_active_tier_not_in_tiers_list() -> None:
    config = _make_config(active_tier_id="missing_tier")
    with pytest.raises(ValueError):
        Task(config=config, tiers=[_make_tier("rules"), _make_tier("cloud_llm")])


def test_task_partitions_tiers_into_cascade_and_escalation() -> None:
    config = _make_config(active_tier_id="local_llm")
    tiers = [
        _make_tier("rules"),
        _make_tier("classifier"),
        _make_tier("local_llm"),
        _make_tier("cloud_llm"),
        _make_tier("human"),
    ]
    task = Task(config=config, tiers=tiers)
    cascade_ids = [t.tier_id for t in task.tiers_up_to_active()]
    escalation_ids = [t.tier_id for t in task.escalation_tiers()]
    assert cascade_ids == ["rules", "classifier", "local_llm"]
    assert escalation_ids == ["cloud_llm", "human"]


def test_task_reality_window_is_timedelta_of_window_days() -> None:
    config = _make_config(reality_observation_window_days=14)
    task = Task(config=config, tiers=[_make_tier("cloud_llm")])
    assert task.reality_window.days == 14


def test_task_config_loads_from_yaml(tmp_path: Path) -> None:
    yaml_text = """
task_id: email_classification
description: Two-layer email classification
input_schema_class: app.workers.email_models.EmailMessage
output_schema_class: app.workers.email_models.EmailClassification
active_tier_id: cloud_llm
escalation_policy: ask_human
reality_observation_window_days: 7
graduation_thresholds:
  rules:
    precision: 0.95
    recall: 0.80
  cloud_llm:
    precision: 0.90
    recall: 0.90
"""
    path = tmp_path / "task.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    config = TaskConfig.from_yaml(path)
    assert config.task_id == "email_classification"
    assert config.graduation_thresholds[TierKind.RULES].precision == 0.95
    assert config.graduation_thresholds[TierKind.CLOUD_LLM].recall == 0.90
