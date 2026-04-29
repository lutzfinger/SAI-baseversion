"""Deterministic hashing helpers for workflow tool definitions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.shared.models import WorkflowToolDefinition
from app.shared.tool_registry import get_tool_spec


def compute_tool_sha256(
    tool_definition: WorkflowToolDefinition,
    *,
    prompt_sha256: str | None = None,
) -> str:
    """Return a deterministic hash for one workflow tool declaration."""

    payload = {
        "tool_id": tool_definition.tool_id,
        "kind": tool_definition.kind,
        "tool_spec_sha256": get_tool_spec(tool_definition.kind).sha256(),
        "enabled": tool_definition.enabled,
        "prompt": tool_definition.prompt,
        "prompt_sha256": prompt_sha256,
        "provider": tool_definition.provider,
        "model": tool_definition.model,
        "config": _normalized_value(tool_definition.config),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalized_value(val) for key, val in sorted(value.items())}
    if isinstance(value, list):
        return [_normalized_value(item) for item in value]
    return value
