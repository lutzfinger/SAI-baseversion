"""Gemini-backed review of LangSmith traces for observability improvements."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.shared.config import Settings

LLM_RUN_TYPES = {"llm", "chat_model", "chat"}
LANGSMITH_LIST_RUNS_MAX_LIMIT = 100


class TracingNodeDigest(BaseModel):
    """Compact redacted summary for one LangSmith node."""

    model_config = ConfigDict(extra="forbid")

    name: str
    run_type: str
    path: str
    status: str
    latency_ms: int | None = None
    total_tokens: int | None = None
    total_cost: float | None = None
    error: str | None = None


class RootTraceDigest(BaseModel):
    """Aggregate view of one root LangSmith trace."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    trace_id: str
    name: str
    workflow_id: str | None = None
    status: str
    started_at: str | None = None
    latency_ms: int | None = None
    total_tokens: int | None = None
    total_cost: float | None = None
    node_count: int
    llm_node_count: int
    tokenized_llm_node_count: int
    tokenless_llm_node_count: int
    error_node_count: int
    slow_nodes: list[TracingNodeDigest] = Field(default_factory=list)
    tokenless_slow_nodes: list[TracingNodeDigest] = Field(default_factory=list)


class TracingFeedbackFinding(BaseModel):
    """One Gemini-generated improvement finding."""

    model_config = ConfigDict(extra="forbid")

    title: str
    severity: Literal["high", "medium", "low"]
    evidence: str
    recommendation: str


class TracingFeedbackReport(BaseModel):
    """Structured Gemini review for one batch of traces."""

    model_config = ConfigDict(extra="forbid")

    reviewed_run_count: int
    executive_summary: str
    top_findings: list[TracingFeedbackFinding] = Field(default_factory=list)
    quick_wins: list[str] = Field(default_factory=list)
    instrumentation_gaps: list[str] = Field(default_factory=list)
    bottlenecks: list[str] = Field(default_factory=list)


class TracingFeedbackState(BaseModel):
    """Persistent state for every-10-runs tracing review batches."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    updated_at: datetime | None = None
    reviewed_root_run_ids: list[str] = Field(default_factory=list)
    review_runs_started: int = 0
    last_review_run_id: str | None = None
    last_batch_root_run_ids: list[str] = Field(default_factory=list)
    last_report_path: str | None = None


class PreparedTracingFeedbackBatch(BaseModel):
    """Selected batch of root traces ready for Gemini review."""

    model_config = ConfigDict(extra="forbid")

    started: bool
    batch_size: int
    pending_root_run_count: int
    selected_root_run_ids: list[str] = Field(default_factory=list)
    artifact_dir: str | None = None
    batch_summary_path: str | None = None
    digests: list[RootTraceDigest] = Field(default_factory=list)


class GeminiTracingReviewer:
    """Use Gemini to critique tracing quality from redacted LangSmith summaries."""

    def __init__(
        self,
        *,
        settings: Settings,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.model = settings.langsmith_tracing_feedback_model
        self.client = client

    def review_batch(
        self,
        *,
        project_name: str,
        digests: list[RootTraceDigest],
    ) -> TracingFeedbackReport:
        client = self.client or _build_gemini_client(self.settings)
        prompt = _render_gemini_prompt(project_name=project_name, digests=digests)
        response = _generate_gemini_review(
            client=client,
            model=self.model,
            prompt=prompt,
        )
        raw_text = str(getattr(response, "text", "") or "").strip()
        if not raw_text:
            raise ValueError("Gemini did not return structured tracing feedback text.")
        return TracingFeedbackReport.model_validate_json(raw_text)


def create_langsmith_client(settings: Settings) -> Any:
    """Create a LangSmith client for read-only trace review."""

    if not settings.langsmith_api_key:
        raise RuntimeError("SAI_LANGSMITH_API_KEY is not configured.")
    from langsmith import Client

    return Client(
        api_url=settings.langsmith_endpoint,
        api_key=settings.langsmith_api_key,
    )


def load_tracing_feedback_state(settings: Settings) -> TracingFeedbackState:
    """Load persisted tracing-review state or start from empty state."""

    path = settings.langsmith_tracing_feedback_state_path
    if not path.exists():
        return TracingFeedbackState()
    return TracingFeedbackState.model_validate_json(path.read_text(encoding="utf-8"))


def save_tracing_feedback_state(
    settings: Settings,
    *,
    state: TracingFeedbackState,
) -> None:
    """Persist tracing-review state."""

    settings.langsmith_tracing_feedback_state_path.write_text(
        state.model_dump_json(indent=2),
        encoding="utf-8",
    )


def prepare_tracing_feedback_batch(
    *,
    settings: Settings,
    client: Any,
    run_id: str,
    batch_size: int | None = None,
) -> PreparedTracingFeedbackBatch:
    """Select the next unread batch of LangSmith root runs and write a summary artifact."""

    effective_batch_size = batch_size or settings.langsmith_tracing_feedback_batch_size
    poll_limit = max(
        1,
        min(
            settings.langsmith_tracing_feedback_poll_limit,
            LANGSMITH_LIST_RUNS_MAX_LIMIT,
        ),
    )
    state = load_tracing_feedback_state(settings)
    reviewed_ids = set(state.reviewed_root_run_ids)
    root_runs = list(
        client.list_runs(
            project_name=settings.langsmith_project,
            is_root=True,
            limit=poll_limit,
        )
    )
    pending_runs = [
        run
        for run in sorted(root_runs, key=_root_sort_key)
        if str(getattr(run, "id", "")) not in reviewed_ids
        and getattr(run, "end_time", None) is not None
    ]
    if len(pending_runs) < effective_batch_size:
        return PreparedTracingFeedbackBatch(
            started=False,
            batch_size=effective_batch_size,
            pending_root_run_count=len(pending_runs),
        )

    selected_runs = pending_runs[:effective_batch_size]
    digests = [
        summarize_root_run(client.read_run(run.id, load_child_runs=True))
        for run in selected_runs
    ]
    prepared_at = datetime.now(UTC)
    artifact_dir = settings.artifacts_dir / (
        f"langsmith_tracing_feedback_{prepared_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    batch_summary_path = artifact_dir / "trace_batch_summary.json"
    batch_summary_path.write_text(
        json.dumps(
            {
                "prepared_at": prepared_at.isoformat(),
                "run_id": run_id,
                "project_name": settings.langsmith_project,
                "batch_size": effective_batch_size,
                "pending_root_run_count": len(pending_runs),
                "selected_root_run_ids": [str(run.id) for run in selected_runs],
                "digests": [digest.model_dump(mode="json") for digest in digests],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return PreparedTracingFeedbackBatch(
        started=True,
        batch_size=effective_batch_size,
        pending_root_run_count=len(pending_runs),
        selected_root_run_ids=[str(run.id) for run in selected_runs],
        artifact_dir=str(artifact_dir),
        batch_summary_path=str(batch_summary_path),
        digests=digests,
    )


def mark_tracing_feedback_reviewed(
    *,
    settings: Settings,
    run_id: str,
    reviewed_root_run_ids: list[str],
    report_path: str,
) -> TracingFeedbackState:
    """Record a completed tracing feedback batch."""

    state = load_tracing_feedback_state(settings)
    reviewed = set(state.reviewed_root_run_ids)
    reviewed.update(reviewed_root_run_ids)
    state.updated_at = datetime.now(UTC)
    state.review_runs_started += 1
    state.last_review_run_id = run_id
    state.last_batch_root_run_ids = list(reviewed_root_run_ids)
    state.last_report_path = report_path
    state.reviewed_root_run_ids = sorted(reviewed)
    save_tracing_feedback_state(settings, state=state)
    return state


def summarize_root_run(root_run: Any) -> RootTraceDigest:
    """Redact and compress one LangSmith root run plus child nodes."""

    nodes = list(_iter_nodes(root_run))
    descendants = nodes[1:]
    llm_nodes = [node for node in descendants if node.run_type in LLM_RUN_TYPES]
    tokenized_llm_nodes = [node for node in llm_nodes if (node.total_tokens or 0) > 0]
    tokenless_llm_nodes = [node for node in llm_nodes if not node.total_tokens]
    error_nodes = [node for node in descendants if node.error]
    slow_nodes = sorted(
        descendants,
        key=lambda node: node.latency_ms or -1,
        reverse=True,
    )[:5]
    tokenless_slow_nodes = [
        node
        for node in sorted(
            tokenless_llm_nodes,
            key=lambda node: node.latency_ms or -1,
            reverse=True,
        )
        if (node.latency_ms or 0) >= 500
    ][:5]
    workflow_id = _workflow_id_from_run(root_run)
    status = _run_status(root_run)
    return RootTraceDigest(
        run_id=str(getattr(root_run, "id", "")),
        trace_id=str(getattr(root_run, "trace_id", "")),
        name=str(getattr(root_run, "name", "")).strip() or "unnamed_run",
        workflow_id=workflow_id,
        status=status,
        started_at=_isoformat_or_none(getattr(root_run, "start_time", None)),
        latency_ms=_latency_ms(root_run),
        total_tokens=_int_or_none(getattr(root_run, "total_tokens", None)),
        total_cost=_float_or_none(getattr(root_run, "total_cost", None)),
        node_count=len(nodes),
        llm_node_count=len(llm_nodes),
        tokenized_llm_node_count=len(tokenized_llm_nodes),
        tokenless_llm_node_count=len(tokenless_llm_nodes),
        error_node_count=len(error_nodes),
        slow_nodes=slow_nodes,
        tokenless_slow_nodes=tokenless_slow_nodes,
    )


def format_tracing_feedback_slack_message(
    *,
    report: TracingFeedbackReport,
    project_name: str,
    model: str,
    artifact_dir: str,
) -> str:
    """Create one concise Slack message for tracing review output."""

    lines = [
        (
            f"Gemini tracing review for `{project_name}` using `{model}` "
            f"on {report.reviewed_run_count} runs."
        ),
        report.executive_summary.strip(),
    ]
    if report.top_findings:
        lines.append("Top findings:")
        for index, finding in enumerate(report.top_findings[:3], start=1):
            lines.append(
                f"{index}. [{finding.severity}] {finding.title}: {finding.recommendation}"
            )
    if report.quick_wins:
        lines.append("Quick wins:")
        for item in report.quick_wins[:3]:
            lines.append(f"- {item}")
    lines.append(f"Artifact: {artifact_dir}")
    message = "\n".join(line.strip() for line in lines if line.strip())
    if len(message) <= 3500:
        return message
    return message[:3497].rstrip() + "..."


def _build_gemini_client(settings: Settings) -> Any:
    if not settings.gemini_api_key:
        raise RuntimeError("SAI_GEMINI_API_KEY is not configured.")
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover - runtime dependency issue
        raise RuntimeError(
            "The google-genai package is not installed. Add it to the environment first."
        ) from exc
    return genai.Client(api_key=settings.gemini_api_key)


def _generate_gemini_review(*, client: Any, model: str, prompt: str) -> Any:
    try:
        from google.genai import types
    except ImportError:
        return client.models.generate_content(
            model=model,
            contents=prompt,
        )
    return client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=_build_gemini_response_schema(
                schema_type=types.Schema,
                model=TracingFeedbackReport,
            ),
        ),
    )


def _build_gemini_response_schema(*, schema_type: Any, model: type[BaseModel]) -> Any:
    schema = model.model_json_schema()
    definitions = _schema_definitions(schema)
    normalized = _normalize_gemini_schema_node(schema, definitions=definitions)
    return schema_type.model_validate(normalized)


def _schema_definitions(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    definitions: dict[str, dict[str, Any]] = {}
    for key in ("$defs", "definitions"):
        raw_definitions = schema.get(key)
        if not isinstance(raw_definitions, dict):
            continue
        for name, definition in raw_definitions.items():
            if isinstance(name, str) and isinstance(definition, dict):
                definitions[name] = definition
    return definitions


def _normalize_gemini_schema_node(
    node: Any,
    *,
    definitions: dict[str, dict[str, Any]],
) -> Any:
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str):
        return _normalize_gemini_schema_node(
            _resolve_schema_reference(ref, definitions=definitions),
            definitions=definitions,
        )

    any_of = node.get("anyOf")
    if isinstance(any_of, list):
        null_variants = [
            item for item in any_of if isinstance(item, dict) and item.get("type") == "null"
        ]
        non_null_variants = [
            item for item in any_of if not (isinstance(item, dict) and item.get("type") == "null")
        ]
        if len(non_null_variants) == 1:
            normalized_variant = _normalize_gemini_schema_node(
                non_null_variants[0],
                definitions=definitions,
            )
            if isinstance(normalized_variant, dict) and null_variants:
                normalized_variant["nullable"] = True
            return normalized_variant
        return {
            "anyOf": [
                _normalize_gemini_schema_node(item, definitions=definitions)
                for item in non_null_variants
            ]
        }

    normalized: dict[str, Any] = {}

    description = node.get("description")
    if isinstance(description, str) and description.strip():
        normalized["description"] = description.strip()

    schema_type = node.get("type")
    if isinstance(schema_type, str) and schema_type != "null":
        normalized["type"] = schema_type

    enum_values = node.get("enum")
    if isinstance(enum_values, list) and all(
        isinstance(value, (str, int, float, bool)) for value in enum_values
    ):
        normalized["enum"] = enum_values
        normalized.setdefault("format", "enum")

    properties = node.get("properties")
    if isinstance(properties, dict):
        normalized["type"] = "object"
        normalized["properties"] = {
            name: _normalize_gemini_schema_node(value, definitions=definitions)
            for name, value in properties.items()
            if isinstance(name, str)
        }
        required = node.get("required")
        if isinstance(required, list):
            normalized["required"] = [name for name in required if isinstance(name, str)]

    items = node.get("items")
    if isinstance(items, dict):
        normalized["type"] = "array"
        normalized["items"] = _normalize_gemini_schema_node(
            items,
            definitions=definitions,
        )

    nullable = node.get("nullable")
    if isinstance(nullable, bool):
        normalized["nullable"] = nullable

    return normalized


def _resolve_schema_reference(
    ref: str,
    *,
    definitions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if ref.startswith("#/$defs/"):
        name = ref.removeprefix("#/$defs/")
    elif ref.startswith("#/definitions/"):
        name = ref.removeprefix("#/definitions/")
    else:
        raise ValueError(f"Unsupported schema reference: {ref}")
    resolved = definitions.get(name)
    if resolved is None:
        raise KeyError(f"Schema definition not found: {name}")
    return resolved


def _render_gemini_prompt(*, project_name: str, digests: list[RootTraceDigest]) -> str:
    return (
        "You are a principal observability reviewer. Analyze these redacted LangSmith "
        "trace summaries for the SAI system.\n"
        "Focus on:\n"
        "- repeated latency bottlenecks\n"
        "- token/cost instrumentation gaps\n"
        "- tracing blind spots in nested nodes\n"
        "- serial graph steps that should be parallelized\n"
        "- prompt or classification drift patterns visible in run names or node timing\n"
        "Use only the provided data. Do not assume hidden inputs.\n"
        "Prefer actionable engineering recommendations over generic advice.\n"
        f"Project: {project_name}\n\n"
        "TRACE_SUMMARY_JSON:\n"
        f"{json.dumps([digest.model_dump(mode='json') for digest in digests], sort_keys=True)}\n"
    )


def _iter_nodes(root_run: Any) -> Iterable[TracingNodeDigest]:
    root_name = str(getattr(root_run, "name", "")).strip() or "unnamed_run"
    yield _node_digest(root_run, path=root_name)
    child_runs = getattr(root_run, "child_runs", None)
    if not isinstance(child_runs, list):
        return
    for child in child_runs:
        yield from _iter_child_nodes(child, path=root_name)


def _iter_child_nodes(run: Any, *, path: str) -> Iterable[TracingNodeDigest]:
    name = str(getattr(run, "name", "")).strip() or "unnamed_run"
    next_path = f"{path} > {name}"
    yield _node_digest(run, path=next_path)
    child_runs = getattr(run, "child_runs", None)
    if not isinstance(child_runs, list):
        return
    for child in child_runs:
        yield from _iter_child_nodes(child, path=next_path)


def _node_digest(run: Any, *, path: str) -> TracingNodeDigest:
    return TracingNodeDigest(
        name=str(getattr(run, "name", "")).strip() or "unnamed_run",
        run_type=str(getattr(run, "run_type", "")).strip() or "unknown",
        path=path,
        status=_run_status(run),
        latency_ms=_latency_ms(run),
        total_tokens=_int_or_none(getattr(run, "total_tokens", None)),
        total_cost=_float_or_none(getattr(run, "total_cost", None)),
        error=_string_or_none(getattr(run, "error", None)),
    )


def _workflow_id_from_run(run: Any) -> str | None:
    extra = getattr(run, "extra", None)
    if not isinstance(extra, dict):
        return None
    metadata = extra.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("workflow_id")
        if value:
            return str(value)
    return None


def _root_sort_key(run: Any) -> datetime:
    start_time = getattr(run, "start_time", None)
    if isinstance(start_time, datetime):
        return start_time
    end_time = getattr(run, "end_time", None)
    if isinstance(end_time, datetime):
        return end_time
    return datetime.min.replace(tzinfo=UTC)


def _latency_ms(run: Any) -> int | None:
    start_time = getattr(run, "start_time", None)
    end_time = getattr(run, "end_time", None)
    if not isinstance(start_time, datetime) or not isinstance(end_time, datetime):
        return None
    return max(0, int((end_time - start_time).total_seconds() * 1000))


def _isoformat_or_none(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _run_status(run: Any) -> str:
    if getattr(run, "error", None):
        return "failed"
    status = getattr(run, "status", None)
    if status is None:
        return "completed"
    return str(status)


def _int_or_none(value: object) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(cast(int | str, value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(cast(float | str, value))
    except (TypeError, ValueError):
        return None


def _string_or_none(value: object) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)
