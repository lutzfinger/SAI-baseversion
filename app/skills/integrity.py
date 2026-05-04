"""Skill integrity — SHA-256 over the skill directory's contents.

Closes the gap operator named: "skills are created in Claude (Co-Work)
and are in Claude. SAI uses them. SAI downloads them and uses them.
How does SAI understand when a skill in Claude has changed."

Answer: hash. At PROMOTION time (the moment a Co-Work-emitted skill
moves from `incoming/<draft_id>/` to `skills/<workflow_id>/`),
Claude Code computes the SHA-256 of the skill's content and writes
it to `.skill-content-sha256` in the skill directory. The skill
loader recomputes on every load and refuses to register a skill
whose contents drift from the recorded hash.

Mechanism mirrors the prompt-locks loader (#24c) one level up: not
"this prompt file matches its locked hash" but "this whole skill
directory matches its promoted hash."

Hash inputs (sorted, deterministic):
  * skill.yaml          (the manifest)
  * runner.py           (the dispatcher)
  * send_tool.py        (if present)
  * canaries.jsonl
  * edge_cases.jsonl
  * workflow_regression.jsonl
  * prompts/**/*.md     (every hash-locked prompt the skill ships)
  * config-diffs/**/*   (config patches the skill bundles)

EXCLUDED (intentionally — these are noise / generated):
  * __init__.py         (empty Python package marker)
  * __pycache__/        (bytecode)
  * MANIFEST.txt        (Co-Work's own manifest of file hashes — the
                         framework computes its own; otherwise
                         circular)
  * README.md           (operator-facing docs; can be edited freely
                         without invalidating the skill itself)
  * .skill-content-sha256 (the file we're writing)
  * .DS_Store, .git*, *.bak  (system noise)

The hash is "what the framework EXECUTES" — not "what's on disk."
"""

from __future__ import annotations

import hashlib
from pathlib import Path

INTEGRITY_FILENAME = ".skill-content-sha256"

_INCLUDED_FILE_NAMES = frozenset({
    "skill.yaml",
    "runner.py",
    "send_tool.py",
    "canaries.jsonl",
    "edge_cases.jsonl",
    "workflow_regression.jsonl",
})

_INCLUDED_SUBDIRS = frozenset({
    "prompts",
    "config-diffs",
})

_EXCLUDED_NAME_SUFFIXES = (".bak", ".pyc", ".pyo", ".DS_Store")
_EXCLUDED_DIR_NAMES = frozenset({"__pycache__", ".git"})


class SkillIntegrityError(RuntimeError):
    """Raised when a skill's recorded sha256 doesn't match its current
    contents — i.e. the skill was edited after promotion."""


def _iter_skill_files(skill_dir: Path) -> list[Path]:
    """Sorted list of files included in the integrity hash."""
    out: list[Path] = []
    for p in sorted(skill_dir.iterdir()):
        if p.is_file() and p.name in _INCLUDED_FILE_NAMES:
            out.append(p)
    for sub in _INCLUDED_SUBDIRS:
        sub_path = skill_dir / sub
        if not sub_path.is_dir():
            continue
        for p in sorted(sub_path.rglob("*")):
            if not p.is_file():
                continue
            if p.name.endswith(_EXCLUDED_NAME_SUFFIXES):
                continue
            if any(part in _EXCLUDED_DIR_NAMES for part in p.parts):
                continue
            out.append(p)
    return out


def compute_skill_sha256(skill_dir: Path) -> str:
    """Compute the integrity hash for a skill directory.

    Deterministic across machines: sorted file list, NUL-delimited
    `<relpath>\\0<sha256-of-bytes>\\0` records hashed end-to-end.
    """

    if not skill_dir.is_dir():
        raise FileNotFoundError(f"skill_dir is not a directory: {skill_dir}")

    h = hashlib.sha256()
    for path in _iter_skill_files(skill_dir):
        rel = path.relative_to(skill_dir).as_posix()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(file_hash.encode("ascii"))
        h.update(b"\x00")
    return h.hexdigest()


def write_integrity_file(skill_dir: Path) -> str:
    """Compute current sha256, write to .skill-content-sha256.

    Returns the hash. Called at promotion time
    (`scripts/promote_skill.py`) and on demand if the operator
    deliberately edits a skill in place.
    """
    sha = compute_skill_sha256(skill_dir)
    (skill_dir / INTEGRITY_FILENAME).write_text(sha + "\n", encoding="utf-8")
    return sha


def read_integrity_file(skill_dir: Path) -> str | None:
    """Return the recorded sha256, or None if the file is missing."""
    path = skill_dir / INTEGRITY_FILENAME
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def verify_skill_integrity(skill_dir: Path, *, strict: bool = True) -> str:
    """Compute current sha256, compare against the recorded one.

    Returns the current sha256 on success. Raises ``SkillIntegrityError``
    on mismatch when ``strict=True`` (default). When ``strict=False``,
    returns the current sha256 even on mismatch — useful for the
    promote tool that's actively rewriting the file.

    Behavior when no recorded hash exists:
      * `strict=True` → raise (skill not promoted properly)
      * `strict=False` → return current sha256 silently
    """
    current = compute_skill_sha256(skill_dir)
    recorded = read_integrity_file(skill_dir)
    if recorded is None:
        if strict:
            raise SkillIntegrityError(
                f"{skill_dir.name}: no .skill-content-sha256 file. Promote "
                f"this skill via scripts.promote_skill or call "
                f"write_integrity_file() to record the current state."
            )
        return current
    if current != recorded:
        if strict:
            raise SkillIntegrityError(
                f"{skill_dir.name}: integrity check FAILED. "
                f"recorded={recorded[:12]}…, current={current[:12]}… — "
                f"the skill's contents changed after promotion. Either "
                f"revert the edit OR re-promote: write_integrity_file()."
            )
    return current
