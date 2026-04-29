"""Prepare threshold-triggered local-model training batches from comparison logs."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.learning.local_cloud_comparison import parse_local_cloud_comparison_payload
from app.shared.config import Settings

TrainingStage = Literal["local_prompt_tuning", "local_lora_fine_tune"]


class LocalModelTrainingRecord(BaseModel):
    """Model-agnostic supervised record derived from one cloud-backed disagreement."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    example_id: str
    task: str = "email_classification"
    input_email: dict[str, Any]
    target_classification: dict[str, Any]
    prior_local_prediction: dict[str, Any]
    keyword_baseline: dict[str, Any] | None = None
    training_target_source: str = "cloud_target"
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocalCloudTrainingStageState(BaseModel):
    """Per-stage persistent reservation state for local model improvement."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    threshold: int
    training_runs_started: int = 0
    reserved_example_ids: list[str] = Field(default_factory=list)
    last_training_run_id: str | None = None
    last_bundle_path: str | None = None


class LocalCloudTrainingState(BaseModel):
    """Persistent multi-stage state for prompt tuning and local LoRA training."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "2"
    updated_at: datetime | None = None
    local_prompt_tuning: LocalCloudTrainingStageState
    local_lora_fine_tune: LocalCloudTrainingStageState


class TrainingBatchPreparation(BaseModel):
    """Summary of one prepared training batch."""

    model_config = ConfigDict(extra="forbid")

    stage: TrainingStage
    started: bool
    threshold: int
    pending_unique_disagreements: int
    selected_example_count: int
    reserved_example_count: int
    artifact_dir: str | None = None
    manifest_path: str | None = None
    dataset_path: str | None = None
    comparison_path: str | None = None
    level1_targets: dict[str, int] = Field(default_factory=dict)
    level2_targets: dict[str, int] = Field(default_factory=dict)


def load_training_state(settings: Settings) -> LocalCloudTrainingState:
    """Load training reservation state or return a clean default."""

    path = settings.local_cloud_training_state_path
    if not path.exists():
        return _fresh_training_state(settings)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "local_prompt_tuning" in payload and "local_lora_fine_tune" in payload:
        return LocalCloudTrainingState.model_validate(payload)
    # Backward compatibility for the previous single-stage state format.
    legacy_reserved = payload.get("reserved_example_ids", [])
    legacy = _fresh_training_state(settings)
    legacy.local_prompt_tuning = legacy.local_prompt_tuning.model_copy(
        update={
            "training_runs_started": int(payload.get("training_runs_started", 0)),
            "reserved_example_ids": list(legacy_reserved),
            "last_training_run_id": payload.get("last_training_run_id"),
            "last_bundle_path": payload.get("last_bundle_path"),
        }
    )
    legacy.updated_at = (
        datetime.fromisoformat(payload["updated_at"])
        if payload.get("updated_at")
        else None
    )
    return legacy


def prepare_training_batch(
    *,
    settings: Settings,
    run_id: str,
    stage: TrainingStage,
    threshold: int | None = None,
) -> TrainingBatchPreparation:
    """Reserve the next batch of unique disagreement examples and write bundle files."""

    stage_state_name = stage
    state = load_training_state(settings)
    stage_state = getattr(state, stage_state_name)
    effective_threshold = threshold or stage_state.threshold
    reserved_ids = set(stage_state.reserved_example_ids)
    pending_records = _pending_training_records(settings, reserved_ids=reserved_ids)
    if len(pending_records) < effective_threshold:
        return TrainingBatchPreparation(
            stage=stage,
            started=False,
            threshold=effective_threshold,
            pending_unique_disagreements=len(pending_records),
            selected_example_count=0,
            reserved_example_count=len(reserved_ids),
        )

    selected_records = pending_records[:effective_threshold]
    prepared_at = datetime.now(UTC)
    artifact_dir = settings.artifacts_dir / (
        f"{stage}_{prepared_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    comparison_path = artifact_dir / "comparison_examples.jsonl"
    dataset_path = artifact_dir / "training_dataset.jsonl"
    manifest_path = artifact_dir / "manifest.json"

    _write_jsonl(
        comparison_path,
        [record.model_dump(mode="json") for record in selected_records],
    )
    _write_jsonl(
        dataset_path,
        [record.model_dump(mode="json") for record in selected_records],
    )

    level1_targets = Counter(
        _label_value(record.target_classification, "level1_classification")
        for record in selected_records
    )
    level2_targets = Counter(
        _label_value(record.target_classification, "level2_intent")
        for record in selected_records
    )
    manifest = {
        "schema_version": "1",
        "stage": stage,
        "prepared_at": prepared_at.isoformat(),
        "run_id": run_id,
        "threshold": effective_threshold,
        "selected_example_count": len(selected_records),
        "pending_unique_disagreements_before_reserve": len(pending_records),
        "comparison_examples_path": str(comparison_path),
        "training_dataset_path": str(dataset_path),
        "level1_targets": dict(sorted(level1_targets.items())),
        "level2_targets": dict(sorted(level2_targets.items())),
        "local_llm_model": settings.local_llm_model,
        "local_prompt_addendum_path": str(settings.local_llm_prompt_addendum_path),
        "training_backend": (
            "local_prompt_tuning"
            if stage == "local_prompt_tuning"
            else settings.local_cloud_finetune_backend
        ),
        "training_command_configured": (
            False
            if stage == "local_prompt_tuning"
            else bool(
                settings.local_cloud_finetune_enabled
                and settings.local_cloud_finetune_command
            )
        ),
        "training_launch_enabled": (
            False if stage == "local_prompt_tuning" else settings.local_cloud_finetune_enabled
        ),
        "training_command": (
            None
            if stage == "local_prompt_tuning"
            else (
                settings.local_cloud_finetune_command
                if settings.local_cloud_finetune_enabled
                else None
            )
        ),
        "finetune_method": (
            None if stage == "local_prompt_tuning" else settings.local_cloud_finetune_method
        ),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    reserved_ids.update(record.example_id for record in selected_records)
    state.updated_at = prepared_at
    setattr(
        state,
        stage_state_name,
        stage_state.model_copy(
            update={
                "training_runs_started": stage_state.training_runs_started + 1,
                "threshold": effective_threshold,
                "last_training_run_id": run_id,
                "last_bundle_path": str(artifact_dir),
                "reserved_example_ids": sorted(reserved_ids),
            }
        ),
    )
    settings.local_cloud_training_state_path.write_text(
        state.model_dump_json(indent=2),
        encoding="utf-8",
    )

    return TrainingBatchPreparation(
        stage=stage,
        started=True,
        threshold=effective_threshold,
        pending_unique_disagreements=len(pending_records),
        selected_example_count=len(selected_records),
        reserved_example_count=len(reserved_ids),
        artifact_dir=str(artifact_dir),
        manifest_path=str(manifest_path),
        dataset_path=str(dataset_path),
        comparison_path=str(comparison_path),
        level1_targets=dict(sorted(level1_targets.items())),
        level2_targets=dict(sorted(level2_targets.items())),
    )


def select_training_stages(settings: Settings) -> list[tuple[TrainingStage, int, int]]:
    """Return the stages that currently have enough fresh disagreements to run."""

    state = load_training_state(settings)
    selections: list[tuple[TrainingStage, int, int]] = []
    for raw_stage_name, configured_threshold in (
        ("local_prompt_tuning", settings.local_cloud_prompt_tuning_threshold),
        ("local_lora_fine_tune", settings.local_cloud_finetune_threshold),
    ):
        stage_name = cast(TrainingStage, raw_stage_name)
        stage_state = getattr(state, stage_name)
        pending = len(
            _pending_training_records(
                settings,
                reserved_ids=set(stage_state.reserved_example_ids),
            )
        )
        if pending >= configured_threshold:
            selections.append((stage_name, configured_threshold, pending))
    return selections


def pending_disagreement_counts(settings: Settings) -> dict[str, int]:
    """Expose current per-stage pending counts for operator reporting."""

    state = load_training_state(settings)
    return {
        "local_prompt_tuning": len(
            _pending_training_records(
                settings,
                reserved_ids=set(state.local_prompt_tuning.reserved_example_ids),
            )
        ),
        "local_lora_fine_tune": len(
            _pending_training_records(
                settings,
                reserved_ids=set(state.local_lora_fine_tune.reserved_example_ids),
            )
        ),
    }


def _fresh_training_state(settings: Settings) -> LocalCloudTrainingState:
    return LocalCloudTrainingState(
        local_prompt_tuning=LocalCloudTrainingStageState(
            threshold=settings.local_cloud_prompt_tuning_threshold
        ),
        local_lora_fine_tune=LocalCloudTrainingStageState(
            threshold=settings.local_cloud_finetune_threshold
        ),
    )


def coerce_training_record(payload: dict[str, Any]) -> LocalModelTrainingRecord:
    """Accept either normalized training rows or legacy comparison examples."""

    try:
        return LocalModelTrainingRecord.model_validate(payload)
    except ValidationError:
        example = parse_local_cloud_comparison_payload(payload)
        return LocalModelTrainingRecord(
            example_id=example.example_id,
            input_email=example.message.model_dump(mode="json"),
            target_classification=example.cloud_target.model_dump(mode="json"),
            prior_local_prediction=example.local_prediction.model_dump(mode="json"),
            keyword_baseline=(
                example.keyword_baseline.model_dump(mode="json")
                if example.keyword_baseline is not None
                else None
            ),
            training_target_source=example.training_target_source,
            metadata={
                "workflow_id": example.workflow_id,
                "run_id": example.run_id,
                "captured_at": example.captured_at.isoformat(),
                "source_kind": example.source_kind,
                "source_artifact_path": example.source_artifact_path,
                "local_prompt": (
                    example.local_prompt.model_dump(mode="json")
                    if example.local_prompt is not None
                    else None
                ),
                "cloud_prompt": (
                    example.cloud_prompt.model_dump(mode="json")
                    if example.cloud_prompt is not None
                    else None
                ),
                "consistency_score": example.consistency_score,
                "langsmith_evaluator": (
                    example.langsmith_evaluator.model_dump(mode="json")
                    if example.langsmith_evaluator is not None
                    else None
                ),
            },
        )


def _pending_training_records(
    settings: Settings,
    *,
    reserved_ids: set[str],
) -> list[LocalModelTrainingRecord]:
    by_example_id: dict[str, LocalModelTrainingRecord] = {}
    path = settings.local_cloud_training_dataset_path
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            record = coerce_training_record(json.loads(line))
            if record.example_id in reserved_ids:
                continue
            by_example_id.setdefault(record.example_id, record)
    return list(by_example_id.values())


def _label_value(payload: dict[str, Any], key: str) -> str:
    return str(payload.get(key, "")).strip()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")
