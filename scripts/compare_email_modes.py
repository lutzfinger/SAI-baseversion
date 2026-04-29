from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.control_plane.runner import ControlPlane
from app.shared.config import get_settings
from app.shared.gmail_token_override import apply_gmail_token_path_override
from app.shared.models import WorkflowToolDefinition
from app.shared.run_ids import new_run_id
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import EmailMessage


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare keyword, local-LLM, and cloud-enabled results on one live inbox slice."
    )
    parser.add_argument("--workflow", default="email-triage-gmail")
    parser.add_argument("--max-results", type=int, default=15)
    parser.add_argument(
        "--gmail-token-path",
        default=None,
        help=(
            "Optional token file path override for live Gmail fetches. "
            "Use this to compare a specific mailbox account."
        ),
    )
    parser.add_argument(
        "--messages-file",
        type=Path,
        help=(
            "Optional JSON snapshot of EmailMessage objects to replay instead "
            "of pulling Gmail live."
        ),
    )
    args = parser.parse_args()

    settings = get_settings()
    apply_gmail_token_path_override(
        workflow_id=args.workflow,
        token_path=args.gmail_token_path,
        settings=settings,
    )
    control_plane = ControlPlane(settings)
    workflow = control_plane.workflow_with_connector_overrides(
        control_plane.workflow_store.load(f"{args.workflow}.yaml"),
        connector_overrides={"max_results": args.max_results},
    )
    policy = control_plane.policy_store.load(workflow.policy)
    prompts_by_tool_id = control_plane._load_tool_prompts(workflow=workflow)
    if args.messages_file is not None:
        messages = _load_messages(args.messages_file)
        messages_path = args.messages_file.resolve()
    else:
        connector = control_plane._build_connector(
            workflow=workflow,
            policy=policy,
            source_override=None,
        )
        messages = connector.fetch_messages()
        messages_path = None

    guard_status = control_plane._resolve_input_guard_status(workflow, policy)
    local_status = control_plane._resolve_local_llm_status(workflow)
    cloud_status = control_plane._resolve_cloud_llm_status(workflow)

    keyword_tools = _filter_tools(
        workflow.tools,
        excluded_kinds={"local_llm_classifier", "escalation_classifier"},
    )
    local_tools = _filter_tools(workflow.tools, excluded_kinds={"escalation_classifier"})
    cloud_tools = list(workflow.tools)

    keyword_run_id = new_run_id(f"{workflow.workflow_id}_keyword_compare")
    with control_plane.langsmith.workflow_trace(
        workflow_id=f"{workflow.workflow_id}-keyword-compare",
        run_id=keyword_run_id,
        redaction_config=policy.redaction,
        extra_metadata={"message_count": len(messages), "compare_mode": "keyword"},
    ):
        keyword_results = control_plane.email_worker.classify_messages(
            run_id=keyword_run_id,
            workflow_id=f"{workflow.workflow_id}-keyword",
            messages=messages,
            prompts_by_tool_id=prompts_by_tool_id,
            tool_definitions=keyword_tools,
            policy=policy,
            operator_email=settings.user_email,
            local_llm_available=False,
            local_llm_reason="local_llm_not_used_in_keyword_mode",
            local_llm_provider=local_status.provider,
            local_llm_model=local_status.model,
            local_llm_host=local_status.host,
            cloud_llm_available=False,
            cloud_llm_reason="cloud_llm_not_used_in_keyword_mode",
            cloud_llm_provider=cloud_status.provider,
            cloud_llm_model=cloud_status.model,
            cloud_llm_host=cloud_status.host,
        )
    local_run_id = new_run_id(f"{workflow.workflow_id}_local_compare")
    with control_plane.langsmith.workflow_trace(
        workflow_id=f"{workflow.workflow_id}-local-compare",
        run_id=local_run_id,
        redaction_config=policy.redaction,
        extra_metadata={"message_count": len(messages), "compare_mode": "local"},
    ):
        local_results = control_plane.email_worker.classify_messages(
            run_id=local_run_id,
            workflow_id=f"{workflow.workflow_id}-local",
            messages=messages,
            prompts_by_tool_id=prompts_by_tool_id,
            tool_definitions=local_tools,
            policy=policy,
            operator_email=settings.user_email,
            local_llm_available=local_status.available,
            local_llm_reason=local_status.reason,
            local_llm_provider=local_status.provider,
            local_llm_model=local_status.model,
            local_llm_host=local_status.host,
            cloud_llm_available=False,
            cloud_llm_reason="cloud_llm_not_used_in_local_mode",
            cloud_llm_provider=cloud_status.provider,
            cloud_llm_model=cloud_status.model,
            cloud_llm_host=cloud_status.host,
        )
    cloud_run_id = new_run_id(f"{workflow.workflow_id}_cloud_compare")
    with control_plane.langsmith.workflow_trace(
        workflow_id=f"{workflow.workflow_id}-cloud-compare",
        run_id=cloud_run_id,
        redaction_config=policy.redaction,
        extra_metadata={"message_count": len(messages), "compare_mode": "cloud"},
    ):
        cloud_results = control_plane.email_worker.classify_messages(
            run_id=cloud_run_id,
            workflow_id=f"{workflow.workflow_id}-cloud",
            messages=messages,
            prompts_by_tool_id=prompts_by_tool_id,
            tool_definitions=cloud_tools,
            policy=policy,
            operator_email=settings.user_email,
            local_llm_available=local_status.available,
            local_llm_reason=local_status.reason,
            local_llm_provider=local_status.provider,
            local_llm_model=local_status.model,
            local_llm_host=local_status.host,
            cloud_llm_available=cloud_status.available,
            cloud_llm_reason=cloud_status.reason,
            cloud_llm_provider=cloud_status.provider,
            cloud_llm_model=cloud_status.model,
            cloud_llm_host=cloud_status.host,
        )

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = settings.artifacts_dir / f"compare_email_modes_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    if messages_path is None:
        messages_path = output_dir / "messages.json"
        messages_path.write_text(
            json.dumps([message.model_dump(mode="json") for message in messages], indent=2),
            encoding="utf-8",
        )

    comparison_payload = _build_comparison_payload(
        workflow_id=workflow.workflow_id,
        messages=messages,
        keyword_results=keyword_results,
        local_results=local_results,
        cloud_results=cloud_results,
        guard_status=asdict(guard_status),
        local_status=asdict(local_status),
        cloud_status=asdict(cloud_status),
        messages_path=messages_path,
    )
    output_path = output_dir / "comparison.json"
    output_path.write_text(json.dumps(comparison_payload, indent=2), encoding="utf-8")
    print(json.dumps(comparison_payload, indent=2))
def _load_messages(path: Path) -> list[EmailMessage]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Message snapshot must be a list of objects: {path}")
    return [EmailMessage.model_validate(item) for item in payload]


def _filter_tools(
    tool_definitions: list[WorkflowToolDefinition],
    *,
    excluded_kinds: set[str],
) -> list[WorkflowToolDefinition]:
    return [tool for tool in tool_definitions if tool.kind not in excluded_kinds]


def _build_comparison_payload(
    *,
    workflow_id: str,
    messages: list[Any],
    keyword_results: list[Any],
    local_results: list[Any],
    cloud_results: list[Any],
    guard_status: dict[str, Any],
    local_status: dict[str, Any],
    cloud_status: dict[str, Any],
    messages_path: Path,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    keyword_meeting = 0
    local_meeting = 0
    cloud_meeting = 0
    keyword_vs_local_changes = 0
    local_vs_cloud_changes = 0
    keyword_vs_local_label_changes = 0
    local_vs_cloud_label_changes = 0
    cloud_completed = 0
    local_completed = 0
    local_fallback = 0
    local_timeout = 0
    input_guard_blocked = 0

    for message, keyword_result, local_result, cloud_result in zip(
        messages,
        keyword_results,
        local_results,
        cloud_results,
        strict=True,
    ):
        keyword_classification = keyword_result.classification
        local_classification = local_result.classification
        cloud_classification = cloud_result.classification

        keyword_signature = _classification_signature(keyword_classification)
        local_signature = _classification_signature(local_classification)
        cloud_signature = _classification_signature(cloud_classification)

        if keyword_classification.meeting_request:
            keyword_meeting += 1
        if local_classification.meeting_request:
            local_meeting += 1
        if cloud_classification.meeting_request:
            cloud_meeting += 1
        if keyword_signature != local_signature:
            keyword_vs_local_changes += 1
        if local_signature != cloud_signature:
            local_vs_cloud_changes += 1
        if _classification_label_signature(
            keyword_classification
        ) != _classification_label_signature(local_classification):
            keyword_vs_local_label_changes += 1
        if _classification_label_signature(
            local_classification
        ) != _classification_label_signature(cloud_classification):
            local_vs_cloud_label_changes += 1

        local_record = _first_tool_record(local_result.tool_records, "local_llm_classifier")
        if local_record is not None:
            if local_record.status is ToolExecutionStatus.COMPLETED:
                local_completed += 1
            elif local_record.status is ToolExecutionStatus.FALLBACK:
                local_fallback += 1
                if str(local_record.details.get("reason", "")).strip().lower() == "timed out":
                    local_timeout += 1

        input_guard_record = _first_tool_record(local_result.tool_records, "input_guard")
        if (
            input_guard_record is not None
            and input_guard_record.status is ToolExecutionStatus.BLOCKED
        ):
            input_guard_blocked += 1

        cloud_record = _first_tool_record(cloud_result.tool_records, "escalation_classifier")
        if cloud_record is not None and cloud_record.status is ToolExecutionStatus.COMPLETED:
            cloud_completed += 1

        rows.append(
            {
                "message_id": message.message_id,
                "thread_id": message.thread_id,
                "from_email": message.from_email,
                "subject": message.subject,
                "keyword": _classification_row(keyword_result.tool_records, keyword_classification),
                "local_llm": _classification_row(local_result.tool_records, local_classification),
                "cloud_enabled": _classification_row(
                    cloud_result.tool_records,
                    cloud_classification,
                ),
                "keyword_vs_local_changed": keyword_signature != local_signature,
                "local_vs_cloud_changed": local_signature != cloud_signature,
            }
        )

    return {
        "workflow_id": workflow_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "input_message_count": len(messages),
        "input_snapshot_path": str(messages_path),
        "runtime_status": {
            "input_guard": guard_status,
            "local_llm": local_status,
            "cloud_llm": cloud_status,
        },
        "summary": {
            "keyword_meeting_request_count": keyword_meeting,
            "local_meeting_request_count": local_meeting,
            "cloud_meeting_request_count": cloud_meeting,
            "keyword_vs_local_changes": keyword_vs_local_changes,
            "local_vs_cloud_changes": local_vs_cloud_changes,
            "keyword_vs_local_label_changes": keyword_vs_local_label_changes,
            "local_vs_cloud_label_changes": local_vs_cloud_label_changes,
            "cloud_completed_count": cloud_completed,
            "local_completed_count": local_completed,
            "local_fallback_count": local_fallback,
            "local_timeout_count": local_timeout,
            "input_guard_blocked_count": input_guard_blocked,
        },
        "items": rows,
    }


def _classification_signature(classification: Any) -> tuple[Any, ...]:
    return (
        classification.meeting_request,
        classification.level1_classification,
        classification.level2_intent,
        round(float(classification.confidence), 4),
    )


def _classification_label_signature(classification: Any) -> tuple[Any, ...]:
    return (
        classification.meeting_request,
        classification.level1_classification,
        classification.level2_intent,
    )


def _classification_row(
    tool_records: list[ToolExecutionRecord],
    classification: Any,
) -> dict[str, Any]:
    return {
        "meeting_request": classification.meeting_request,
        "level1_classification": classification.level1_classification,
        "level2_intent": classification.level2_intent,
        "confidence": classification.confidence,
        "reason": classification.reason,
        "tool_statuses": {
            record.tool_kind: record.status.value for record in tool_records
        },
        "input_guard_details": _tool_details(tool_records, "input_guard"),
        "local_llm_details": _tool_details(tool_records, "local_llm_classifier"),
        "escalation_details": _tool_details(tool_records, "escalation_classifier"),
    }


def _first_tool_record(
    tool_records: list[ToolExecutionRecord],
    tool_kind: str,
) -> ToolExecutionRecord | None:
    return next((record for record in tool_records if record.tool_kind == tool_kind), None)


def _tool_details(
    tool_records: list[ToolExecutionRecord],
    tool_kind: str,
) -> dict[str, Any] | None:
    record = _first_tool_record(tool_records, tool_kind)
    if record is None:
        return None
    return record.details


if __name__ == "__main__":
    main()
