from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from app.control_plane.runner import ControlPlane
from app.learning.local_cloud_comparison import (
    ComparisonOutcome,
    LocalCloudComparisonExample,
    LocalCloudComparisonRecorder,
    ModelPromptReference,
    ModelRuntimeReference,
    ToolExecutionSnapshot,
    build_local_cloud_example_id,
)
from app.shared.config import get_settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.workers.email_models import (
    EmailClassification,
    EmailMessage,
    Level1Classification,
    Level2Intent,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay saved local-vs-cloud comparison artifacts into the learning dataset."
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=None,
        help="Optional artifacts root override. Defaults to settings.artifacts_dir.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of comparison artifacts to replay.",
    )
    args = parser.parse_args()

    settings = get_settings()
    control_plane = ControlPlane(settings)
    recorder = LocalCloudComparisonRecorder(settings=settings)
    artifacts_root = args.artifacts_dir or settings.artifacts_dir
    comparison_paths = sorted(artifacts_root.glob("compare_email_modes_*/comparison.json"))
    if args.limit is not None:
        comparison_paths = comparison_paths[: args.limit]

    total_examples = 0
    total_recorded = 0
    total_duplicates = 0
    skipped_items = 0
    artifact_summaries: list[dict[str, object]] = []

    for comparison_path in comparison_paths:
        payload = json.loads(comparison_path.read_text(encoding="utf-8"))
        messages_path = Path(str(payload["input_snapshot_path"]))
        messages = {
            message.message_id: message
            for message in _load_messages(messages_path)
        }
        workflow_id = str(payload["workflow_id"])
        workflow = control_plane.workflow_store.load(f"{workflow_id}.yaml")
        prompts_by_tool_id = control_plane._load_tool_prompts(workflow=workflow)

        examples: list[LocalCloudComparisonExample] = []
        for row in payload.get("items", []):
            if not isinstance(row, dict):
                skipped_items += 1
                continue
            message_id = str(row.get("message_id", "")).strip()
            message = messages.get(message_id)
            if message is None:
                skipped_items += 1
                continue
            example = _build_example_from_artifact_row(
                row=row,
                message=message,
                workflow_id=workflow_id,
                artifact_path=comparison_path,
                captured_at=datetime.fromisoformat(str(payload["generated_at"])),
                run_id=comparison_path.parent.name,
                prompts_by_tool_id=prompts_by_tool_id,
                workflow_tools=workflow.tools,
                local_runtime=_runtime_reference(payload["runtime_status"]["local_llm"]),
                cloud_runtime=_runtime_reference(payload["runtime_status"]["cloud_llm"]),
            )
            if example is None:
                skipped_items += 1
                continue
            examples.append(example)

        summary = recorder.record_examples(examples=examples, dedupe_existing=True)
        total_examples += summary["received"]
        total_recorded += summary["recorded"]
        total_duplicates += summary["duplicates_skipped"]
        artifact_summaries.append(
            {
                "artifact_path": str(comparison_path),
                "examples_seen": summary["received"],
                "examples_recorded": summary["recorded"],
                "duplicates_skipped": summary["duplicates_skipped"],
            }
        )

    result = {
        "artifacts_scanned": len(comparison_paths),
        "examples_seen": total_examples,
        "examples_recorded": total_recorded,
        "duplicates_skipped": total_duplicates,
        "items_skipped": skipped_items,
        "comparison_log_path": str(settings.local_cloud_comparison_log_path),
        "training_dataset_path": str(settings.local_cloud_training_dataset_path),
        "stats_path": str(settings.local_cloud_stats_path),
        "artifacts": artifact_summaries,
    }
    print(json.dumps(result, indent=2))
    return 0


def _load_messages(path: Path) -> list[EmailMessage]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Email snapshot must be a list: {path}")
    return [EmailMessage.model_validate(item) for item in payload]


def _build_example_from_artifact_row(
    *,
    row: dict[str, Any],
    message: EmailMessage,
    workflow_id: str,
    artifact_path: Path,
    captured_at: datetime,
    run_id: str,
    prompts_by_tool_id: dict[str, PromptDocument],
    workflow_tools: list[WorkflowToolDefinition],
    local_runtime: ModelRuntimeReference,
    cloud_runtime: ModelRuntimeReference,
) -> LocalCloudComparisonExample | None:
    keyword_row = row.get("keyword")
    local_row = row.get("local_llm")
    cloud_row = row.get("cloud_enabled")
    if not isinstance(keyword_row, dict) or not isinstance(local_row, dict) or not isinstance(
        cloud_row, dict
    ):
        return None

    local_status = _tool_status(local_row, "local_llm_classifier")
    cloud_status = _tool_status(cloud_row, "escalation_classifier")
    if local_status != "completed" or cloud_status != "completed":
        return None

    local_prediction = _classification_from_row(local_row, message.message_id)
    cloud_target = _classification_from_row(cloud_row, message.message_id)
    keyword_baseline = _classification_from_row(keyword_row, message.message_id)
    if local_prediction is None or cloud_target is None or keyword_baseline is None:
        return None
    local_prompt = _prompt_reference(prompts_by_tool_id, workflow_tools, "local_llm_classifier")
    cloud_prompt = _prompt_reference(prompts_by_tool_id, workflow_tools, "escalation_classifier")
    output_guard_status = _tool_status(cloud_row, "output_guard") or _tool_status(
        local_row, "output_guard"
    )

    return LocalCloudComparisonExample(
        example_id=build_local_cloud_example_id(
            message=message,
            local_prediction=local_prediction,
            cloud_target=cloud_target,
        ),
        captured_at=captured_at,
        run_id=run_id,
        workflow_id=workflow_id,
        source_kind="artifact_replay",
        source_artifact_path=str(artifact_path),
        message=message,
        keyword_baseline=keyword_baseline,
        local_prediction=local_prediction,
        cloud_target=cloud_target,
        final_classification=cloud_target,
        disagreement=ComparisonOutcome(
            overall_disagreement=(
                local_prediction.level1_classification != cloud_target.level1_classification
                or local_prediction.level2_intent != cloud_target.level2_intent
            ),
            level1_disagreement=(
                local_prediction.level1_classification != cloud_target.level1_classification
            ),
            level2_disagreement=(local_prediction.level2_intent != cloud_target.level2_intent),
        ),
        local_runtime=local_runtime,
        cloud_runtime=cloud_runtime,
        local_prompt=local_prompt,
        cloud_prompt=cloud_prompt,
        local_tool=ToolExecutionSnapshot(
            status=local_status,
            details=_row_details(local_row, "local_llm_classifier"),
        ),
        cloud_tool=ToolExecutionSnapshot(
            status=cloud_status,
            details=_row_details(cloud_row, "escalation_classifier"),
        ),
        output_guard=(
            ToolExecutionSnapshot(
                status=output_guard_status,
                details={},
            )
            if output_guard_status
            else None
        ),
    )


def _classification_from_row(
    row: dict[str, object],
    message_id: str,
) -> EmailClassification | None:
    if not {
        "level1_classification",
        "level2_intent",
        "confidence",
        "reason",
    }.issubset(row):
        return None
    return EmailClassification(
        message_id=message_id,
        level1_classification=cast(Level1Classification, row["level1_classification"]),
        level2_intent=cast(Level2Intent, row["level2_intent"]),
        confidence=float(cast(float | int | str, row["confidence"])),
        reason=str(row["reason"]),
    )


def _tool_status(row: dict[str, object], tool_kind: str) -> str | None:
    statuses = row.get("tool_statuses")
    if not isinstance(statuses, dict):
        return None
    status = statuses.get(tool_kind)
    return str(status) if status is not None else None


def _row_details(row: dict[str, object], tool_kind: str) -> dict[str, object]:
    if tool_kind == "local_llm_classifier":
        details = row.get("local_llm_details")
    elif tool_kind == "escalation_classifier":
        details = row.get("escalation_details")
    else:
        details = None
    return details if isinstance(details, dict) else {}


def _prompt_reference(
    prompts_by_tool_id: dict[str, PromptDocument],
    workflow_tools: list[WorkflowToolDefinition],
    kind: str,
) -> ModelPromptReference | None:
    for tool in workflow_tools:
        if tool.kind != kind or not tool.enabled:
            continue
        prompt = prompts_by_tool_id.get(tool.tool_id)
        if prompt is None:
            return None
        return ModelPromptReference(
            tool_id=tool.tool_id,
            prompt_path=tool.prompt or "",
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
        )
    return None


def _runtime_reference(payload: object) -> ModelRuntimeReference:
    raw = payload if isinstance(payload, dict) else {}
    return ModelRuntimeReference(
        provider=str(raw.get("provider")) if raw.get("provider") is not None else None,
        model=str(raw.get("model")) if raw.get("model") is not None else None,
        host=str(raw.get("host")) if raw.get("host") is not None else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
