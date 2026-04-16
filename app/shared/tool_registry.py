"""Runtime source of truth for tool capabilities, controls, and provenance."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from functools import lru_cache
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from app.shared.config import REPO_ROOT
from app.shared.models import PolicyMode
from app.shared.registry import (
    get_effect_class,
    list_effect_classes,
    list_task_kinds,
    load_tool_registry_payload,
)


class ToolSurface(StrEnum):
    WORKFLOW_CALLABLE = "workflow_callable"
    CONNECTOR_ACTION = "connector_action"
    INTERNAL_HELPER = "internal_helper"


ToolEffectLevel = str


class ToolPromptRequirement(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    FORBIDDEN = "forbidden"


class ToolApprovalBehavior(StrEnum):
    ALWAYS_DENIED = "always_denied"
    APPROVAL_SUPPORTED = "approval_supported"


class ToolDataSensitivity(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    RESTRICTED = "restricted"


class PromptProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_path: str
    prompt_lock_sha256: str | None
    last_verification_source: str


class ToolSpec(BaseModel):
    """Authoritative runtime metadata for one workflow-exposed tool kind."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    surface: ToolSurface
    category: str
    purpose: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    effect_class: str
    required_actions: list[str] = Field(default_factory=list)
    default_policy_mode: PolicyMode
    allowed_connectors: list[str] = Field(default_factory=list)
    allowed_targets: list[str] = Field(default_factory=list)
    prompt_requirement: ToolPromptRequirement = ToolPromptRequirement.FORBIDDEN
    default_prompt: str | None = None
    failure_modes: list[str] = Field(default_factory=list)
    redaction_rules: list[str] = Field(default_factory=list)
    approval_behavior: ToolApprovalBehavior = ToolApprovalBehavior.ALWAYS_DENIED
    supports_preview: bool = False
    retryable: bool = False
    idempotent: bool = False
    data_sensitivity: ToolDataSensitivity
    last_verification_source: str = "runtime_tool_registry"

    def sha256(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def list_tool_specs(*, surface: ToolSurface | None = None) -> list[ToolSpec]:
    specs = list(_tool_specs())
    if surface is None:
        return specs
    return [spec for spec in specs if spec.surface is surface]


def get_tool_spec(kind: str) -> ToolSpec:
    try:
        return _tool_specs_by_kind()[kind]
    except KeyError as error:
        raise ValueError(f"Unknown workflow tool kind: {kind}") from error


def validate_workflow_tools(workflow: Any) -> None:
    """Validate one workflow definition against the runtime tool registry."""

    for tool in getattr(workflow, "tools", []):
        if not getattr(tool, "enabled", True):
            continue
        spec = get_tool_spec(tool.kind)
        if spec.surface is ToolSurface.INTERNAL_HELPER:
            raise ValueError(
                f"Tool {tool.tool_id} ({tool.kind}) is internal-only and cannot be declared "
                "directly in workflows."
            )
        if (
            spec.allowed_connectors
            and getattr(workflow, "connector", "") not in spec.allowed_connectors
        ):
            raise ValueError(
                f"Tool {tool.tool_id} ({tool.kind}) is not allowed for connector "
                f"{getattr(workflow, 'connector', '')}."
            )
        prompt = getattr(tool, "prompt", None)
        if spec.prompt_requirement is ToolPromptRequirement.REQUIRED and not prompt:
            raise ValueError(f"Tool {tool.tool_id} ({tool.kind}) requires a prompt reference.")
        if spec.prompt_requirement is ToolPromptRequirement.FORBIDDEN and prompt:
            raise ValueError(
                f"Tool {tool.tool_id} ({tool.kind}) must not define a prompt reference."
            )


def validate_workflow_policy_against_specs(*, workflow: Any, policy: Any) -> None:
    """Ensure runtime policy behavior stays compatible with the tool registry."""

    for tool in getattr(workflow, "tools", []):
        if not getattr(tool, "enabled", True):
            continue
        spec = get_tool_spec(tool.kind)
        for action in spec.required_actions:
            effective_mode = policy.mode_for(action)
            if (
                spec.approval_behavior is ToolApprovalBehavior.ALWAYS_DENIED
                and effective_mode is PolicyMode.APPROVAL_REQUIRED
            ):
                raise ValueError(
                    f"Tool {tool.tool_id} ({tool.kind}) does not allow approval_required "
                    f"for action {action}."
                )
            if effective_mode is not spec.default_policy_mode:
                raise ValueError(
                    f"Tool {tool.tool_id} ({tool.kind}) expects policy mode "
                    f"{spec.default_policy_mode.value} for action {action}, "
                    f"but workflow policy resolved to {effective_mode.value}."
                )


def tool_spec_sha256_map(workflow: Any) -> dict[str, str]:
    return {
        tool.tool_id: get_tool_spec(tool.kind).sha256()
        for tool in getattr(workflow, "tools", [])
        if getattr(tool, "enabled", True)
    }


def render_tool_overview_markdown() -> str:
    """Generate the operator-facing tool overview from the runtime registry."""

    validate_tool_registry_integrity()
    lines: list[str] = [
        "# SAI Tool Overview",
        "",
        f"This document is generated from the registry files in `{REPO_ROOT / 'registry'}`.",
        "",
        "The registry layer is the source of truth for tool metadata, task kinds, "
        "and effect classes. This markdown is the generated operator-facing view.",
        "",
        "## Effect Classes",
        "",
    ]
    for effect in list_effect_classes():
        lines.extend(
            [
                f"### {effect.name}",
                effect.description,
                "",
                f"- external side effects: `{str(effect.has_external_side_effects).lower()}`",
                f"- human review recommended: `{str(effect.human_review_recommended).lower()}`",
                "",
            ]
        )
    lines.extend(["## Task Kinds", ""])
    for task_kind in list_task_kinds():
        lines.extend(
            [
                f"### {task_kind.kind}",
                task_kind.description,
                "",
                "Typical Sources:",
            ]
        )
        if task_kind.typical_sources:
            lines.extend([f"- `{source}`" for source in task_kind.typical_sources])
        else:
            lines.append("- `none`")
        lines.extend(["", "Closure States:"])
        if task_kind.closure_states:
            lines.extend([f"- `{status}`" for status in task_kind.closure_states])
        else:
            lines.append("- `none`")
        lines.append("")
    lines.extend(["## Workflow-callable Tools", ""])
    lines.extend(_render_spec_section(ToolSurface.WORKFLOW_CALLABLE))
    lines.extend(["## Connector-backed Action Tools", ""])
    lines.extend(_render_spec_section(ToolSurface.CONNECTOR_ACTION))
    lines.extend(["## Internal-only Helpers", ""])
    lines.extend(_render_spec_section(ToolSurface.INTERNAL_HELPER))
    lines.extend(["## Shared Schemas", ""])
    for name, schema in _shared_schemas().items():
        lines.extend(
            [
                f"### COMMON OBJECT: {name}",
                "```json",
                json.dumps(schema, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_spec_section(surface: ToolSurface) -> list[str]:
    lines: list[str] = []
    current_category: str | None = None
    sorted_specs = sorted(
        list_tool_specs(surface=surface),
        key=lambda item: (item.category, item.kind),
    )
    for spec in sorted_specs:
        if spec.category != current_category:
            current_category = spec.category
            lines.extend([f"### {current_category}", ""])
        lines.extend(
            [
                f"#### TOOL: {spec.kind}",
                "Purpose:",
                spec.purpose,
                "",
                "Input:",
                "```json",
                json.dumps(spec.input_schema, indent=2, sort_keys=True),
                "```",
                "",
                "Output:",
                "```json",
                json.dumps(spec.output_schema, indent=2, sort_keys=True),
                "```",
                "",
                f"Effect Class: `{spec.effect_class}`",
                "",
                "Required Actions:",
            ]
        )
        if spec.required_actions:
            lines.extend([f"- `{action}`" for action in spec.required_actions])
        else:
            lines.append("- `none`")
        lines.extend(
            [
                "",
                f"Default Policy Mode: `{spec.default_policy_mode.value}`",
                "",
                "Allowed Workflow Connectors:",
            ]
        )
        if spec.allowed_connectors:
            lines.extend([f"- `{connector}`" for connector in spec.allowed_connectors])
        else:
            lines.append("- `any`")
        lines.extend(["", "Allowed Targets:"])
        if spec.allowed_targets:
            lines.extend([f"- `{target}`" for target in spec.allowed_targets])
        else:
            lines.append("- `none`")
        lines.extend(
            [
                "",
                "Prompt Handling:",
                f"- requirement: `{spec.prompt_requirement.value}`",
                f"- default prompt: `{spec.default_prompt or 'none'}`",
                "",
                "Operational Flags:",
                f"- supports_preview: `{str(spec.supports_preview).lower()}`",
                f"- retryable: `{str(spec.retryable).lower()}`",
                f"- idempotent: `{str(spec.idempotent).lower()}`",
                f"- data_sensitivity: `{spec.data_sensitivity.value}`",
                "",
                "Failure Modes:",
            ]
        )
        if spec.failure_modes:
            lines.extend([f"- `{mode}`" for mode in spec.failure_modes])
        else:
            lines.append("- `none`")
        lines.extend(["", "Redaction Rules:"])
        if spec.redaction_rules:
            lines.extend([f"- `{rule}`" for rule in spec.redaction_rules])
        else:
            lines.append("- `none`")
        lines.extend(
            [
                "",
                "Human Approval:",
                f"- `{spec.approval_behavior.value}`",
                "",
                "Tool Provenance:",
                f"- tool-definition SHA256: `{spec.sha256()}`",
                f"- last verification source: `{spec.last_verification_source}`",
                "",
            ]
        )
        prompt_provenance = _prompt_provenance(spec)
        if prompt_provenance is not None:
            lines.extend(
                [
                    "Prompt Provenance:",
                    f"- prompt path: `{prompt_provenance.prompt_path}`",
                    f"- prompt lock SHA256: `{prompt_provenance.prompt_lock_sha256 or 'missing'}`",
                    f"- tool-definition SHA256: `{spec.sha256()}`",
                    f"- last verification source: `{prompt_provenance.last_verification_source}`",
                    "",
                ]
            )
    return lines


def validate_tool_registry_integrity() -> None:
    """Fail fast when the runtime registry drifts from real prompt/runtime files."""

    list_effect_classes()
    list_task_kinds()
    seen: set[str] = set()
    prompt_locks = _prompt_locks()
    for spec in list_tool_specs():
        if spec.kind in seen:
            raise ValueError(f"Duplicate tool registry entry: {spec.kind}")
        seen.add(spec.kind)
        get_effect_class(spec.effect_class)
        if spec.prompt_requirement is ToolPromptRequirement.REQUIRED:
            if not spec.default_prompt:
                raise ValueError(f"Prompt-backed tool {spec.kind} is missing default_prompt.")
            prompt_path = REPO_ROOT / "prompts" / spec.default_prompt
            if not prompt_path.exists():
                raise ValueError(
                    f"Prompt-backed tool {spec.kind} points to a missing prompt: {prompt_path}"
                )
            if spec.default_prompt not in prompt_locks:
                raise ValueError(
                    f"Prompt-backed tool {spec.kind} is missing a prompt lock entry: "
                    f"{spec.default_prompt}"
                )
        if (
            spec.prompt_requirement is ToolPromptRequirement.FORBIDDEN
            and spec.default_prompt is not None
        ):
            raise ValueError(f"Tool {spec.kind} forbids prompts but defines one.")


def _prompt_provenance(spec: ToolSpec) -> PromptProvenance | None:
    if spec.prompt_requirement is not ToolPromptRequirement.REQUIRED or not spec.default_prompt:
        return None
    return PromptProvenance(
        prompt_path=str(REPO_ROOT / "prompts" / spec.default_prompt),
        prompt_lock_sha256=_prompt_locks().get(spec.default_prompt),
        last_verification_source=str(REPO_ROOT / "prompts" / "prompt-locks.yaml"),
    )


@lru_cache(maxsize=1)
def _prompt_locks() -> dict[str, str]:
    path = REPO_ROOT / "prompts" / "prompt-locks.yaml"
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    prompts = payload.get("prompts", {}) if isinstance(payload, dict) else {}
    if not isinstance(prompts, dict):
        return {}
    return {
        str(key).strip(): str(value).strip().lower()
        for key, value in prompts.items()
        if str(key).strip() and str(value).strip()
    }


@lru_cache(maxsize=1)
def _shared_schemas() -> dict[str, dict[str, Any]]:
    payload = load_tool_registry_payload()
    shared = payload.get("shared_schemas", {})
    if not isinstance(shared, dict):
        raise ValueError("registry/tools.yaml must define shared_schemas as a mapping.")
    return shared


@lru_cache(maxsize=1)
def _tool_specs() -> tuple[ToolSpec, ...]:
    payload = load_tool_registry_payload()
    raw_tools = payload.get("tools", [])
    if not isinstance(raw_tools, list):
        raise ValueError("registry/tools.yaml must define tools as a list.")
    specs = tuple(ToolSpec.model_validate(item) for item in raw_tools)
    return specs


@lru_cache(maxsize=1)
def _tool_specs_by_kind() -> dict[str, ToolSpec]:
    specs = _tool_specs()
    return {spec.kind: spec for spec in specs}
