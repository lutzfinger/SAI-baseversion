"""Capture local-vs-cloud email classifications for later training and analysis."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.learning.training_data_notifier import post_training_data_update
from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import EmailClassification, EmailMessage, EmailTriageResult

LOGGER = logging.getLogger(__name__)


class ModelPromptReference(BaseModel):
    """Traceability metadata for the prompt behind one compared model."""

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    prompt_path: str
    prompt_id: str
    prompt_version: str
    prompt_sha256: str


class ModelRuntimeReference(BaseModel):
    """Traceability metadata for one compared model runtime."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model: str | None = None
    host: str | None = None


class ToolExecutionSnapshot(BaseModel):
    """Minimal persisted tool-trace metadata for comparison examples."""

    model_config = ConfigDict(extra="forbid")

    status: str
    details: dict[str, Any] = Field(default_factory=dict)


class ComparisonOutcome(BaseModel):
    """Structured local-vs-cloud disagreement flags."""

    model_config = ConfigDict(extra="forbid")

    overall_disagreement: bool
    level1_disagreement: bool
    level2_disagreement: bool


class LocalCloudComparisonExample(BaseModel):
    """Canonical comparison row for prompt redesign and future local LoRA training."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    example_id: str
    captured_at: datetime
    run_id: str
    workflow_id: str
    source_kind: str = "workflow_run"
    source_artifact_path: str | None = None
    message: EmailMessage
    keyword_baseline: EmailClassification | None = None
    local_prediction: EmailClassification
    cloud_target: EmailClassification
    final_classification: EmailClassification
    disagreement: ComparisonOutcome
    local_runtime: ModelRuntimeReference
    cloud_runtime: ModelRuntimeReference
    local_prompt: ModelPromptReference | None = None
    cloud_prompt: ModelPromptReference | None = None
    local_tool: ToolExecutionSnapshot
    cloud_tool: ToolExecutionSnapshot
    output_guard: ToolExecutionSnapshot | None = None
    langsmith_evaluator: ToolExecutionSnapshot | None = None
    consistency_score: float | None = None
    training_target_source: str = "cloud_target"


def record_local_cloud_comparisons_best_effort(
    *,
    settings: Settings,
    run_id: str,
    workflow_id: str,
    messages: list[EmailMessage],
    results: list[EmailTriageResult],
    prompts_by_tool_id: dict[str, PromptDocument],
    tool_definitions: list[WorkflowToolDefinition],
    local_runtime: ModelRuntimeReference,
    cloud_runtime: ModelRuntimeReference,
) -> None:
    """Append comparison rows without ever breaking the main workflow path."""

    try:
        recorder = LocalCloudComparisonRecorder(settings=settings)
        recorder.record_batch(
            run_id=run_id,
            workflow_id=workflow_id,
            messages=messages,
            results=results,
            prompts_by_tool_id=prompts_by_tool_id,
            tool_definitions=tool_definitions,
            local_runtime=local_runtime,
            cloud_runtime=cloud_runtime,
        )
    except Exception:  # pragma: no cover - best effort by design
        LOGGER.exception(
            "Failed to record local/cloud comparison dataset for run %s workflow %s",
            run_id,
            workflow_id,
        )


class LocalCloudComparisonRecorder:
    """Persist compared examples plus disagreement-focused training candidates."""

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings

    def record_batch(
        self,
        *,
        run_id: str,
        workflow_id: str,
        messages: list[EmailMessage],
        results: list[EmailTriageResult],
        prompts_by_tool_id: dict[str, PromptDocument],
        tool_definitions: list[WorkflowToolDefinition],
        local_runtime: ModelRuntimeReference,
        cloud_runtime: ModelRuntimeReference,
    ) -> None:
        examples = [
            example
            for message, result in zip(messages, results, strict=True)
            if (
                example := self._build_example(
                    run_id=run_id,
                    workflow_id=workflow_id,
                    message=message,
                    result=result,
                    prompts_by_tool_id=prompts_by_tool_id,
                    tool_definitions=tool_definitions,
                    local_runtime=local_runtime,
                    cloud_runtime=cloud_runtime,
                )
            )
            is not None
        ]
        self.record_examples(examples=examples)

    def record_examples(
        self,
        *,
        examples: list[LocalCloudComparisonExample],
        dedupe_existing: bool = False,
    ) -> dict[str, int]:
        """Append ready-made examples and refresh the rolling stats snapshot."""

        if not examples:
            return {"received": 0, "recorded": 0, "duplicates_skipped": 0}

        existing_ids = self._existing_example_ids() if dedupe_existing else set()
        duplicates_skipped = 0
        new_examples: list[LocalCloudComparisonExample] = []
        for example in examples:
            if dedupe_existing and example.example_id in existing_ids:
                duplicates_skipped += 1
                continue
            new_examples.append(example)
            existing_ids.add(example.example_id)

        if not new_examples:
            return {
                "received": len(examples),
                "recorded": 0,
                "duplicates_skipped": duplicates_skipped,
            }

        self._append_jsonl(
            self.settings.local_cloud_comparison_log_path,
            [example.model_dump(mode="json") for example in new_examples],
        )

        disagreement_examples = [
            example for example in new_examples if example.disagreement.overall_disagreement
        ]
        if disagreement_examples:
            disagreement_rows = [
                example.model_dump(mode="json") for example in disagreement_examples
            ]
            self._append_jsonl(
                self.settings.local_cloud_training_dataset_path,
                disagreement_rows,
            )
            post_training_data_update(
                settings=self.settings,
                bucket="cloud_target",
                rows=disagreement_rows,
                duplicates_skipped=duplicates_skipped,
            )

        stats = self._load_stats()
        for example in new_examples:
            self._update_stats(stats, example)
        stats["updated_at"] = datetime.now(UTC).isoformat()
        self.settings.local_cloud_stats_path.write_text(
            json.dumps(stats, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {
            "received": len(examples),
            "recorded": len(new_examples),
            "duplicates_skipped": duplicates_skipped,
        }

    def _build_example(
        self,
        *,
        run_id: str,
        workflow_id: str,
        message: EmailMessage,
        result: EmailTriageResult,
        prompts_by_tool_id: dict[str, PromptDocument],
        tool_definitions: list[WorkflowToolDefinition],
        local_runtime: ModelRuntimeReference,
        cloud_runtime: ModelRuntimeReference,
    ) -> LocalCloudComparisonExample | None:
        comparison = result.comparison
        if (
            comparison is None
            or comparison.local_candidate is None
            or comparison.cloud_candidate is None
        ):
            return None

        local_tool_record = _tool_record(result.tool_records, "local_llm_classifier")
        cloud_tool_record = _tool_record(result.tool_records, "escalation_classifier")
        if local_tool_record is None or cloud_tool_record is None:
            return None
        if (
            local_tool_record.status.value != "completed"
            or cloud_tool_record.status.value != "completed"
        ):
            return None

        local_prompt = _prompt_reference(
            prompts_by_tool_id=prompts_by_tool_id,
            tool_definitions=tool_definitions,
            kind="local_llm_classifier",
        )
        cloud_prompt = _prompt_reference(
            prompts_by_tool_id=prompts_by_tool_id,
            tool_definitions=tool_definitions,
            kind="escalation_classifier",
        )
        output_guard = _tool_record(result.tool_records, "output_guard")
        evaluator = _tool_record(result.tool_records, "langsmith_evaluator")
        consistency_score = None
        if evaluator is not None:
            raw_score = evaluator.details.get("score")
            if isinstance(raw_score, int | float):
                consistency_score = float(raw_score)

        return LocalCloudComparisonExample(
            example_id=build_local_cloud_example_id(
                message=message,
                local_prediction=comparison.local_candidate,
                cloud_target=comparison.cloud_candidate,
            ),
            captured_at=datetime.now(UTC),
            run_id=run_id,
            workflow_id=workflow_id,
            message=message,
            keyword_baseline=comparison.keyword_baseline,
            local_prediction=comparison.local_candidate,
            cloud_target=comparison.cloud_candidate,
            final_classification=result.classification,
            disagreement=ComparisonOutcome(
                overall_disagreement=_classification_signature(comparison.local_candidate)
                != _classification_signature(comparison.cloud_candidate),
                level1_disagreement=(
                    comparison.local_candidate.level1_classification
                    != comparison.cloud_candidate.level1_classification
                ),
                level2_disagreement=(
                    comparison.local_candidate.level2_intent
                    != comparison.cloud_candidate.level2_intent
                ),
            ),
            local_runtime=local_runtime,
            cloud_runtime=cloud_runtime,
            local_prompt=local_prompt,
            cloud_prompt=cloud_prompt,
            local_tool=ToolExecutionSnapshot(
                status=local_tool_record.status.value,
                details=local_tool_record.details,
            ),
            cloud_tool=ToolExecutionSnapshot(
                status=cloud_tool_record.status.value,
                details=cloud_tool_record.details,
            ),
            output_guard=(
                ToolExecutionSnapshot(
                    status=output_guard.status.value,
                    details=output_guard.details,
                )
                if output_guard is not None
                else None
            ),
            langsmith_evaluator=(
                ToolExecutionSnapshot(
                    status=evaluator.status.value,
                    details=evaluator.details,
                )
                if evaluator is not None
                else None
            ),
            consistency_score=consistency_score,
        )

    def _existing_example_ids(self) -> set[str]:
        example_ids: set[str] = set()
        for path in (
            self.settings.local_cloud_comparison_log_path,
            self.settings.local_cloud_training_dataset_path,
        ):
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    example_id = str(payload.get("example_id", "")).strip()
                    if example_id:
                        example_ids.add(example_id)
        return example_ids

    def _append_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True))
                handle.write("\n")

    def _load_stats(self) -> dict[str, Any]:
        if not self.settings.local_cloud_stats_path.exists():
            return {
                "schema_version": "1",
                "updated_at": None,
                "total_compared_examples": 0,
                "total_agreement_examples": 0,
                "total_disagreement_examples": 0,
                "training_candidate_examples": 0,
                "level1": _blank_dimension_stats(),
                "level2": _blank_dimension_stats(),
                "workflows": {},
            }
        return dict(
            json.loads(
                self.settings.local_cloud_stats_path.read_text(encoding="utf-8")
            )
        )

    def _update_stats(self, stats: dict[str, Any], example: LocalCloudComparisonExample) -> None:
        stats["total_compared_examples"] += 1
        if example.disagreement.overall_disagreement:
            stats["total_disagreement_examples"] += 1
            stats["training_candidate_examples"] += 1
        else:
            stats["total_agreement_examples"] += 1

        workflow_stats = stats["workflows"].setdefault(
            example.workflow_id,
            {
                "compared_examples": 0,
                "agreement_examples": 0,
                "disagreement_examples": 0,
            },
        )
        workflow_stats["compared_examples"] += 1
        if example.disagreement.overall_disagreement:
            workflow_stats["disagreement_examples"] += 1
        else:
            workflow_stats["agreement_examples"] += 1

        _update_dimension_stats(
            stats["level1"],
            local_label=example.local_prediction.level1_classification,
            cloud_label=example.cloud_target.level1_classification,
        )
        _update_dimension_stats(
            stats["level2"],
            local_label=example.local_prediction.level2_intent,
            cloud_label=example.cloud_target.level2_intent,
        )


def build_local_cloud_stats(
    *,
    examples: list[LocalCloudComparisonExample],
    updated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return the persisted stats snapshot for a cleaned comparison corpus."""

    stats = {
        "schema_version": "1",
        "updated_at": None,
        "total_compared_examples": 0,
        "total_agreement_examples": 0,
        "total_disagreement_examples": 0,
        "training_candidate_examples": 0,
        "level1": _blank_dimension_stats(),
        "level2": _blank_dimension_stats(),
        "workflows": {},
    }
    recorder = LocalCloudComparisonRecorder(settings=Settings())
    for example in examples:
        recorder._update_stats(stats, example)
    stats["updated_at"] = (updated_at or datetime.now(UTC)).isoformat()
    return stats


def _blank_dimension_stats() -> dict[str, Any]:
    return {
        "compared_examples": 0,
        "agreement_examples": 0,
        "disagreement_examples": 0,
        "agreement_by_label": {},
        "disagreement_by_local_to_cloud": {},
        "disagreement_by_cloud_label": {},
    }


def _update_dimension_stats(
    stats: dict[str, Any],
    *,
    local_label: str,
    cloud_label: str,
) -> None:
    stats["compared_examples"] += 1
    if local_label == cloud_label:
        stats["agreement_examples"] += 1
        agreement_by_label = stats["agreement_by_label"]
        agreement_by_label[cloud_label] = agreement_by_label.get(cloud_label, 0) + 1
        return

    stats["disagreement_examples"] += 1
    confusion = stats["disagreement_by_local_to_cloud"]
    confusion_key = f"{local_label}->{cloud_label}"
    confusion[confusion_key] = confusion.get(confusion_key, 0) + 1
    by_cloud_label = stats["disagreement_by_cloud_label"]
    by_cloud_label[cloud_label] = by_cloud_label.get(cloud_label, 0) + 1


def _prompt_reference(
    *,
    prompts_by_tool_id: dict[str, PromptDocument],
    tool_definitions: list[WorkflowToolDefinition],
    kind: str,
) -> ModelPromptReference | None:
    tool_definition = next(
        (tool for tool in tool_definitions if tool.kind == kind and tool.enabled),
        None,
    )
    if tool_definition is None:
        return None
    prompt = prompts_by_tool_id.get(tool_definition.tool_id)
    if prompt is None:
        return None
    return ModelPromptReference(
        tool_id=tool_definition.tool_id,
        prompt_path=tool_definition.prompt or "",
        prompt_id=prompt.prompt_id,
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
    )


def _tool_record(
    tool_records: list[ToolExecutionRecord],
    kind: str,
) -> ToolExecutionRecord | None:
    for record in tool_records:
        if record.tool_kind == kind:
            return record
    return None


def _classification_signature(classification: EmailClassification) -> tuple[str, str]:
    return (
        classification.level1_classification,
        classification.level2_intent,
    )


def build_local_cloud_example_id(
    *,
    message: EmailMessage,
    local_prediction: EmailClassification,
    cloud_target: EmailClassification,
) -> str:
    """Return a stable content-based key for one disagreement candidate."""

    normalized_payload = {
        "message_id": message.message_id,
        "thread_id": message.thread_id,
        "from_email": message.from_email,
        "subject": message.subject,
        "snippet": message.snippet,
        "body_excerpt": message.body_excerpt,
        "local_level1": local_prediction.level1_classification,
        "local_level2": local_prediction.level2_intent,
        "cloud_level1": cloud_target.level1_classification,
        "cloud_level2": cloud_target.level2_intent,
    }
    digest = hashlib.sha256(
        json.dumps(normalized_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"lcx_{digest[:24]}"


def parse_local_cloud_comparison_payload(payload: dict[str, Any]) -> LocalCloudComparisonExample:
    """Load one comparison row while backfilling fields added after initial launch."""

    if "example_id" not in payload:
        message = EmailMessage.model_validate(payload["message"])
        local_prediction = EmailClassification.model_validate(payload["local_prediction"])
        cloud_target = EmailClassification.model_validate(payload["cloud_target"])
        payload = {
            "source_kind": "workflow_run",
            "source_artifact_path": None,
            **payload,
            "example_id": build_local_cloud_example_id(
                message=message,
                local_prediction=local_prediction,
                cloud_target=cloud_target,
            ),
        }
    return LocalCloudComparisonExample.model_validate(payload)
