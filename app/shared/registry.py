"""Central registry loaders for tools, task kinds, and effect classes."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from app.shared.config import REPO_ROOT

_REGISTRY_ROOT = REPO_ROOT / "registry"


class EffectClassDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    has_external_side_effects: bool = False
    human_review_recommended: bool = False


class TaskKindDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    description: str
    typical_sources: list[str] = Field(default_factory=list)
    closure_states: list[str] = Field(default_factory=list)


@lru_cache(maxsize=1)
def list_effect_classes() -> list[EffectClassDefinition]:
    payload = _load_yaml_mapping(_REGISTRY_ROOT / "effect_classes.yaml")
    raw_items = payload.get("effect_classes", [])
    if not isinstance(raw_items, list):
        raise ValueError("registry/effect_classes.yaml must define an effect_classes list.")
    items = [EffectClassDefinition.model_validate(item) for item in raw_items]
    _assert_unique_names([item.name for item in items], subject="effect class")
    return items


@lru_cache(maxsize=1)
def effect_class_names() -> set[str]:
    return {item.name for item in list_effect_classes()}


def get_effect_class(name: str) -> EffectClassDefinition:
    normalized = name.strip()
    for item in list_effect_classes():
        if item.name == normalized:
            return item
    raise ValueError(f"Unknown effect class: {name}")


@lru_cache(maxsize=1)
def list_task_kinds() -> list[TaskKindDefinition]:
    payload = _load_yaml_mapping(_REGISTRY_ROOT / "task_kinds.yaml")
    raw_items = payload.get("task_kinds", [])
    if not isinstance(raw_items, list):
        raise ValueError("registry/task_kinds.yaml must define a task_kinds list.")
    items = [TaskKindDefinition.model_validate(item) for item in raw_items]
    _assert_unique_names([item.kind for item in items], subject="task kind")
    return items


@lru_cache(maxsize=1)
def task_kind_names() -> set[str]:
    return {item.kind for item in list_task_kinds()}


def get_task_kind(name: str) -> TaskKindDefinition:
    normalized = name.strip()
    for item in list_task_kinds():
        if item.kind == normalized:
            return item
    raise ValueError(f"Unknown task kind: {name}")


def validate_task_kind_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    get_task_kind(normalized)
    return normalized


@lru_cache(maxsize=1)
def load_tool_registry_payload() -> dict[str, Any]:
    payload = _load_yaml_mapping(_REGISTRY_ROOT / "tools.yaml")
    shared = payload.get("shared_schemas", {})
    tools = payload.get("tools", [])
    if not isinstance(shared, dict):
        raise ValueError("registry/tools.yaml must define a shared_schemas mapping.")
    if not isinstance(tools, list):
        raise ValueError("registry/tools.yaml must define a tools list.")
    return {
        "shared_schemas": cast(dict[str, dict[str, Any]], shared),
        "tools": cast(list[dict[str, Any]], tools),
    }


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return cast(dict[str, Any], loaded)


def _assert_unique_names(names: list[str], *, subject: str) -> None:
    seen: set[str] = set()
    for name in names:
        normalized = name.strip()
        if normalized in seen:
            raise ValueError(f"Duplicate {subject} registry entry: {normalized}")
        seen.add(normalized)
