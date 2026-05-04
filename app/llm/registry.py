"""LLM registry accessor (PRINCIPLES.md §24b).

Code references LLMs by **logical role** (e.g. ``agent_default``,
``safety_gate_high``) — never by literal model id. The registry maps
roles to (vendor, model, tier) per ``config/llm_registry.yaml``.

Operators swap a model by editing the YAML; no code change.

A logical role is uniquely identified across the codebase. New roles
land in the YAML registry; the loader does NOT default-create roles.
Asking for an unknown role raises ``UnknownLLMRole`` — fail closed
per #6.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict

from app.shared.config import REPO_ROOT


REGISTRY_PATH: Path = REPO_ROOT / "config" / "llm_registry.yaml"


Tier = Literal["low", "medium", "high"]


class LLMSpec(BaseModel):
    """One entry in the registry: which (vendor, model) at which tier."""

    model_config = ConfigDict(extra="forbid")

    role: str
    vendor: str
    model: str
    tier: Tier
    description: str = ""


class UnknownLLMRole(KeyError):
    """Raised when a code path requests a role that's not in the registry."""


@lru_cache(maxsize=1)
def _load() -> dict[str, LLMSpec]:
    if not REGISTRY_PATH.exists():
        return {}
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    roles_block = raw.get("roles", {}) or {}
    if not isinstance(roles_block, dict):
        return {}
    out: dict[str, LLMSpec] = {}
    for role, body in roles_block.items():
        if not isinstance(body, dict):
            continue
        out[str(role)] = LLMSpec(role=str(role), **body)
    return out


def reload() -> None:
    """Force re-read of the registry. Mostly for tests."""
    _load.cache_clear()


def get(role: str) -> LLMSpec:
    """Look up a role. Raises UnknownLLMRole if missing."""
    spec = _load().get(role)
    if spec is None:
        known = sorted(_load().keys())
        raise UnknownLLMRole(
            f"LLM role {role!r} is not in config/llm_registry.yaml. "
            f"Known roles: {', '.join(known) if known else '(empty registry)'}."
        )
    return spec


def all_roles() -> dict[str, LLMSpec]:
    """Snapshot of every role in the registry (for sai-health + audit)."""
    return dict(_load())


def get_model_for_role(role: str, *, env_override: Optional[str] = None) -> str:
    """Return the concrete model id for ``role``, honoring an env override.

    Used by call sites that want a single string (e.g. building a
    ChatAnthropic). The env override exists ONLY to allow tests /
    operator one-off experiments to swap a single role at runtime
    without editing the registry. Production reads the registry.
    """
    if env_override:
        return env_override
    return get(role).model
