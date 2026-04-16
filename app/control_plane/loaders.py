"""Load external prompts, policies, and workflow definitions from disk.

This file is the core of the "externalized configuration" requirement from the
original plan. Instead of hard-coding prompts or policies inside Python, the
stores here resolve versioned files and return typed runtime documents.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from app.shared.models import PolicyDocument, PromptDocument, WorkflowDefinition
from app.shared.tool_registry import validate_workflow_tools


class PromptStore:
    """Load prompt files with frontmatter metadata and traceability hashes."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, reference: str | Path) -> PromptDocument:
        """Resolve a prompt path, parse frontmatter, and compute its content hash."""

        path = _resolve_path(self.root, reference)
        raw = path.read_text(encoding="utf-8")
        frontmatter, instructions = _split_frontmatter(raw)
        metadata = _yaml_mapping(frontmatter)
        config = {
            key: value
            for key, value in metadata.items()
            if key not in {"prompt_id", "version", "description"}
        }
        return PromptDocument(
            prompt_id=str(metadata.get("prompt_id", path.stem)),
            version=str(metadata.get("version", "1")),
            description=_optional_str(metadata.get("description")),
            instructions=instructions.strip(),
            config=config,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            path=path,
        )


class PromptLockStore:
    """Load the central prompt lock manifest used by workflow tools."""

    def __init__(self, root: Path, filename: str = "prompt-locks.yaml") -> None:
        self.root = root
        self.filename = filename

    def load(self) -> dict[str, str]:
        path = self.root / self.filename
        metadata = _yaml_mapping(path.read_text(encoding="utf-8"))
        prompts = metadata.get("prompts", {})
        if not isinstance(prompts, dict):
            raise ValueError("Prompt lock manifest must define a 'prompts' mapping.")
        locks: dict[str, str] = {}
        for key, value in prompts.items():
            reference = str(key).strip()
            sha256 = str(value).strip().lower()
            if not reference or not sha256:
                continue
            locks[reference] = sha256
        return locks


class PolicyStore:
    """Load policy files that govern side effects and approval requirements."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, reference: str | Path) -> PolicyDocument:
        """Parse a YAML policy file into a typed policy document."""

        path = _resolve_path(self.root, reference)
        metadata = _yaml_mapping(path.read_text(encoding="utf-8"))
        return PolicyDocument.model_validate(
            {
                "policy_id": metadata.get("policy_id", path.stem),
                "version": str(metadata.get("version", "1")),
                "description": metadata.get("description"),
                "default_mode": metadata.get("default_mode", "deny"),
                "rules": metadata.get("rules", []),
                "redaction": metadata.get("redaction", {}),
                "gmail": metadata.get("gmail", {}),
                "calendar": metadata.get("calendar", {}),
                "slack": metadata.get("slack", {}),
                "web": metadata.get("web", {}),
                "path": path,
            }
        )


class WorkflowStore:
    """Load workflow definitions that bind workers, connectors, prompts, and policies."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, reference: str | Path) -> WorkflowDefinition:
        """Return one workflow definition from the versioned workflow store."""

        path = _resolve_path(self.root, reference)
        metadata = _yaml_mapping(path.read_text(encoding="utf-8"))
        workflow = WorkflowDefinition.model_validate(
            {
                "workflow_id": metadata.get("workflow_id", path.stem),
                "version": str(metadata.get("version", "1")),
                "description": metadata["description"],
                "worker": metadata["worker"],
                "connector": metadata["connector"],
                "connector_config": metadata.get("connector_config", {}),
                "prompt": metadata.get("prompt"),
                "policy": metadata["policy"],
                "sample_source": metadata.get("sample_source"),
                "tags": metadata.get("tags", []),
                "tools": metadata.get("tools", []),
                "path": path,
            }
        )
        validate_workflow_tools(workflow)
        return workflow

    def list_workflows(self) -> list[WorkflowDefinition]:
        """List all workflow definitions that the local dashboard can expose."""

        return [self.load(path) for path in sorted(self.root.glob("*.y*ml"))]


def _resolve_path(root: Path, reference: str | Path) -> Path:
    """Resolve relative file references against the configured store root."""

    candidate = Path(reference)
    if candidate.is_absolute():
        return candidate
    resolved = root / candidate
    if resolved.exists():
        return resolved
    raise FileNotFoundError(f"Unable to resolve {reference} within {root}")


def _split_frontmatter(raw: str) -> tuple[str, str]:
    """Split a markdown prompt file into YAML frontmatter and instructions."""

    if not raw.startswith("---\n"):
        return "", raw
    _, remainder = raw.split("---\n", 1)
    frontmatter, instructions = remainder.split("\n---\n", 1)
    return frontmatter, instructions


def _yaml_mapping(raw: str) -> dict[str, Any]:
    """Parse YAML and enforce that the result is a mapping."""

    if not raw.strip():
        return {}
    loaded = yaml.safe_load(raw)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("Expected a YAML mapping")
    return cast(dict[str, Any], loaded)


def _optional_str(value: object) -> str | None:
    """Normalize metadata fields that may be omitted in config files."""

    if value is None:
        return None
    return str(value)
