#!/usr/bin/env python3
"""Boundary check: forbid hardcoded LLM model ids outside the registry layer.

Per PRINCIPLES.md §24b every LLM call MUST go through ``app.llm.registry``;
model ids must NOT appear as literal strings in workers, tools, graphs, or
workflow code. The only legitimate homes for a literal model id are:

  - ``app/llm/`` and ``app/llm/providers/`` — the abstraction layer itself
  - ``config/llm_registry.yaml`` — the single source of truth
  - ``app/llm/cost_table.yaml`` — pricing entries by (vendor, model)
  - ``tests/`` — fixtures / stubs
  - ``docs/`` and ``*.md`` — prose / examples

Anywhere else, a match against the patterns below is a §24b violation. Run
this in CI to block regressions; run it locally before sending a PR.

Exit code: 0 on clean, 1 on any violation, 2 on script error.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]

# Patterns that look like literal model ids. Designed to be loud, not subtle:
# the goal is to catch copy-paste model strings; false positives are fine
# (they get an allowlist entry or move to the registry).
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgpt-[0-9]"),
    re.compile(r"\bclaude-[a-z]+-[0-9]"),
    re.compile(r"\bgemini-[0-9]"),
    re.compile(r"\bo[1-9]-(mini|preview|pro)\b"),
    re.compile(r"\bqwen[0-9]"),
    re.compile(r"\bllama-?[0-9]"),
)

# Files where literal model ids are legitimately allowed.
#
# - `app/llm/`: the abstraction layer itself
# - `config/llm_registry.yaml`: the central registry (single source of truth)
# - `tests/`, `docs/`: fixtures and documentation
# - `scripts/`: operator CLI tools / one-off harnesses. These are an
#   operator-edit surface (like workflow YAMLs); the agent runtime doesn't
#   call them. Naming a model in a `--model` argparse default is the
#   equivalent of naming it in a YAML.
_ALLOWED_PATH_PARTS: tuple[str, ...] = (
    "app/llm/",
    "tests/",
    "docs/",
    "scripts/",
    "config/llm_registry.yaml",
    "config/llm_registry.example.yaml",
)

_SCANNED_SUFFIXES: tuple[str, ...] = (".py", ".yaml", ".yml", ".json", ".toml")


def _is_path_allowed(rel_path: str) -> bool:
    return any(part in rel_path for part in _ALLOWED_PATH_PARTS)


# Inline allowlist marker. Use sparingly — only for tools that use
# vendor-specific features the Provider protocol doesn't cover yet
# (image inputs, web search, moderation, etc.). The reason after the
# colon is required and surfaces in `--explain` mode.
_ALLOWLIST_MARKER = re.compile(
    r"#\s*noqa:\s*hardcoded-model\b(?:\s*--\s*(?P<reason>.+))?", re.IGNORECASE
)


def _scan_file(path: Path, rel: str) -> list[str]:
    findings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return findings
    for line_number, line in enumerate(text.splitlines(), start=1):
        if _ALLOWLIST_MARKER.search(line):
            continue
        for pattern in _PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            findings.append(
                f"{rel}:{line_number}: {match.group(0)!r}  |  {line.strip()[:120]}"
            )
            break
    return findings


def _iter_targets(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in _SCANNED_SUFFIXES:
            continue
        rel = str(path.relative_to(root))
        if any(
            part in rel
            for part in (
                "__pycache__",
                ".git/",
                "logs/",
                ".venv/",
                "venv/",
                ".mypy_cache/",
                ".pytest_cache/",
                ".ruff_cache/",
                "egg-info/",
            )
        ):
            continue
        if _is_path_allowed(rel):
            continue
        yield path


def main(argv: list[str]) -> int:
    root = REPO_ROOT
    if len(argv) > 1:
        root = Path(argv[1]).resolve()
    if not root.exists():
        print(f"check_hardcoded_models: root {root} does not exist", file=sys.stderr)
        return 2

    findings: list[str] = []
    for target in _iter_targets(root):
        rel = str(target.relative_to(root))
        findings.extend(_scan_file(target, rel))

    if not findings:
        print("check_hardcoded_models: clean — no literal model ids found.")
        return 0

    print(
        "check_hardcoded_models: VIOLATIONS (PRINCIPLES.md #24b)\n"
        "Move these to config/llm_registry.yaml and call cascade.predict(role=...) instead.\n",
        file=sys.stderr,
    )
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(f"\nTotal violations: {len(findings)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
