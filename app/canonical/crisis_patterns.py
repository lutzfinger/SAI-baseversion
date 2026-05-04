"""Crisis pattern matcher (PRINCIPLES.md §6 fail-closed).

Hard-stop deterministic check for self-harm / suicide / immediate-
danger language. Anything matching MUST bypass the classifier and
escalate to a human — auto-replying to someone in crisis is wrong
even with empathetic language.

The patterns themselves live in private overlay
(``config/crisis_patterns.yaml``) so the operator can tune them
without code changes. The matcher (this module) is public.

Conservative-broad design: false positives go to the operator,
which is the right failure mode. Fix recall first, precision
second.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from app.shared.config import REPO_ROOT


CRISIS_PATTERNS_PATH: Path = REPO_ROOT / "config" / "crisis_patterns.yaml"


@lru_cache(maxsize=1)
def _patterns() -> list[re.Pattern[str]]:
    if not CRISIS_PATTERNS_PATH.exists():
        return []
    raw = yaml.safe_load(CRISIS_PATTERNS_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return []
    block = raw.get("patterns", []) or []
    out: list[re.Pattern[str]] = []
    for entry in block:
        if not isinstance(entry, str):
            continue
        s = entry.strip()
        if not s or s.startswith("#"):
            continue
        try:
            out.append(re.compile(s, re.IGNORECASE))
        except re.error:
            # Malformed pattern: skip but don't crash the whole worker.
            continue
    return out


def reload() -> None:
    _patterns.cache_clear()


def matches_crisis(text: str) -> list[str]:
    """Return a list of matched pattern strings (for audit). Empty
    list = no match. Callers MUST escalate on any non-empty result.

    The pattern source string is returned (not the regex object) so
    audit logs are human-readable.
    """

    if not text:
        return []
    matched: list[str] = []
    for pat in _patterns():
        if pat.search(text):
            matched.append(pat.pattern)
    return matched
