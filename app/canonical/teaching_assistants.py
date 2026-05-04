"""Canonical TA roster loader (PRINCIPLES.md §17 — public mechanism).

Reads the operator's TA roster from
``config/teaching_assistants.yaml`` (private overlay; gitignored
from public). Used by the cornell-delay-triage skill to select
which TAs to CC on auto-replies.

Schema (per-TA):
  display_name      — for greeting in the reply body
  email             — TA's email address (used in CC)
  course_id         — must reference an existing course in
                      courses.yaml
  active_terms      — list of term labels (e.g. ["Spring 2026"]) —
                      reply only CCs TAs whose active_terms includes
                      the course's current_term
  last_verified     — ISO date the operator last confirmed the TA
                      info is current

Fail-closed (per #6):
  * Missing file              → loader returns empty registry
  * Missing required field    → raises at load time
  * Stale roster              → ``is_roster_stale_for_course`` True
                                 when ALL TAs for a course have
                                 last_verified older than the
                                 ta_roster_max_age_days runtime
                                 tunable (default 180 days)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.shared.config import REPO_ROOT


TA_ROSTER_PATH: Path = REPO_ROOT / "config" / "teaching_assistants.yaml"


class TeachingAssistant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., min_length=5, max_length=320)
    course_id: str = Field(..., min_length=1, max_length=80)
    active_terms: list[str] = Field(
        default_factory=list,
        description="If empty, TA is treated as INACTIVE for any term.",
    )
    last_verified: date = Field(...)

    @field_validator("email", mode="after")
    @classmethod
    def _email_has_at(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError(f"email must contain '@': {v!r}")
        return v

    def is_active_for_term(self, term_label: str) -> bool:
        return term_label in self.active_terms


def _max_age_days() -> int:
    try:
        from app.shared.runtime_tunables import get as _t
        return int(_t("ta_roster_max_age_days"))
    except Exception:
        return 180


@lru_cache(maxsize=1)
def _load() -> list[TeachingAssistant]:
    if not TA_ROSTER_PATH.exists():
        return []
    raw = yaml.safe_load(TA_ROSTER_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return []
    block = raw.get("teaching_assistants", []) or []
    out: list[TeachingAssistant] = []
    for entry in block:
        out.append(TeachingAssistant.model_validate(entry))
    return out


def reload() -> None:
    """Force re-read; mostly for tests."""
    _load.cache_clear()


def all_tas() -> list[TeachingAssistant]:
    return list(_load())


def get_active_tas_for_course(
    course_id: str, term_label: str,
) -> list[TeachingAssistant]:
    """Return TAs whose course_id matches AND whose active_terms
    includes the current term. Empty list = no active TAs (caller
    should escalate, not guess)."""

    return [
        ta for ta in _load()
        if ta.course_id == course_id and ta.is_active_for_term(term_label)
    ]


def is_roster_stale_for_course(
    course_id: str, today: Optional[date] = None,
) -> bool:
    """True if EVERY TA assigned to course_id has a last_verified
    older than the tunable window. The conservative read: if no TA
    was recently verified, the whole roster for that course is
    suspect.
    """

    today = today or datetime.now(UTC).date()
    threshold = _max_age_days()
    course_tas = [ta for ta in _load() if ta.course_id == course_id]
    if not course_tas:
        return True  # no TAs assigned at all — also "stale" in the
                     # sense that we can't proceed
    return all(
        (today - ta.last_verified).days > threshold
        for ta in course_tas
    )
