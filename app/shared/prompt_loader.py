"""Hash-verifying prompt loader (PRINCIPLES.md §23 + §24c).

A small fail-closed loader that any non-cascade caller (agents,
second-opinion gate, future skill prompts) can use without spinning up
the whole control-plane PromptStore + Verifier stack.

Behavior:
  * Reads the file at ``REPO_ROOT/prompts/<relpath>``
  * Computes SHA-256 of the raw bytes
  * Looks up ``relpath`` in ``REPO_ROOT/prompts/prompt-locks.yaml``
  * Fails closed (raises ``PromptHashMismatch``) if the lock entry
    is missing OR the actual hash differs
  * Strips the YAML frontmatter (``---\\n...\\n---\\n``) and returns
    the body string

When merged-runtime mode is in use, ``REPO_ROOT`` from
``app.shared.config`` resolves to whatever directory the runtime is
launched from (the launchd plist sets WorkingDirectory to
``~/.sai-runtime``), so the same code reads the merged copy in
production and the public copy in tests.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from app.shared.config import REPO_ROOT


class PromptHashMismatch(RuntimeError):
    """Raised when a prompt's bytes don't match the locked SHA-256."""


_FRONTMATTER_DELIM = "---"


@lru_cache(maxsize=1)
def _locks() -> dict[str, str]:
    path = REPO_ROOT / "prompts" / "prompt-locks.yaml"
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    prompts = payload.get("prompts", {}) if isinstance(payload, dict) else {}
    if not isinstance(prompts, dict):
        return {}
    return {
        str(k).strip(): str(v).strip().lower()
        for k, v in prompts.items()
        if str(k).strip() and str(v).strip()
    }


def reload_locks() -> None:
    """Force re-read of prompt-locks.yaml (mostly for tests)."""

    _locks.cache_clear()


def load_hashed_prompt(relpath: str, *, prompts_root: Optional[Path] = None) -> str:
    """Load + hash-verify the prompt body at ``prompts/<relpath>``.

    ``prompts_root`` is injectable for tests; production passes None.

    Returns the prompt body with YAML frontmatter stripped. Raises
    ``PromptHashMismatch`` on any failure (missing lock entry, missing
    file, hash mismatch). Per PRINCIPLES.md §6 fail-closed: callers do
    NOT degrade silently; they surface the failure to the operator.
    """

    root = prompts_root or (REPO_ROOT / "prompts")
    path = root / relpath
    if not path.exists():
        raise PromptHashMismatch(
            f"Prompt file missing: {path} (relpath={relpath!r})"
        )

    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    expected = _locks().get(relpath)

    if expected is None:
        raise PromptHashMismatch(
            f"Prompt {relpath!r} has no entry in prompts/prompt-locks.yaml. "
            f"Add it with sha256={actual} after reviewing the content."
        )
    if expected.lower() != actual.lower():
        raise PromptHashMismatch(
            f"Prompt {relpath!r} hash mismatch — "
            f"locked={expected[:12]}…, actual={actual[:12]}…. "
            f"Either revert the file or refresh prompts/prompt-locks.yaml."
        )

    return _strip_frontmatter(raw.decode("utf-8"))


def _strip_frontmatter(text: str) -> str:
    """Strip a leading ``---\\n...\\n---\\n`` YAML frontmatter block.

    No frontmatter? Returns the text unchanged.
    """

    if not text.startswith(_FRONTMATTER_DELIM):
        return text.strip() + "\n"
    rest = text[len(_FRONTMATTER_DELIM):]
    # Find the closing ``---`` on its own line
    closing = rest.find(f"\n{_FRONTMATTER_DELIM}")
    if closing < 0:
        return text.strip() + "\n"
    body_start = closing + 1 + len(_FRONTMATTER_DELIM)
    body = rest[body_start:].lstrip("\n")
    return body
