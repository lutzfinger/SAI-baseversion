"""Canonical course memory loader (PRINCIPLES.md §17 — public mechanism).

Reads the operator's course catalog from ``config/courses.yaml``
(private overlay; gitignored from public). Used by the
cornell-delay-triage skill (e1) and any future skill that needs to
reason about which course an email refers to + what the late-work
policy is.

Schema (per-course):
  course_id              — short id (e.g. operator-internal short code)
  display_name           — human-readable name
  identifiers            — list of strings the body-text matcher
                           looks for (course numbers, common short
                           names, etc.) — case-insensitive
  late_work_policy       — multi-line policy text used in the
                           SAI-authored reply
  policy_last_verified   — ISO date the operator last confirmed
                           the policy_text is current
  current_term           — string label (e.g. "Spring 2026")
  term_start             — ISO date
  term_end               — ISO date
  from_address           — operator's official course email
                           (the address the auto-reply is sent FROM)

Fail-closed (per #6):
  * Missing file        → loader returns empty registry; consumers
                          fail with friendly "no courses configured".
  * Malformed entry     → raises at load time (loud, never silent).
  * Stale policy        → ``is_policy_stale(course)`` returns True
                          when ``policy_last_verified`` is older than
                          ``course_policy_max_age_days`` runtime
                          tunable (default 180 days).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.shared.config import REPO_ROOT


COURSES_PATH: Path = REPO_ROOT / "config" / "courses.yaml"


class Course(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(..., min_length=1, max_length=80)
    display_name: str = Field(..., min_length=1, max_length=200)
    identifiers: list[str] = Field(
        default_factory=list,
        description="Case-insensitive substrings searched in email body. "
                    "Empty list = course-agnostic profile (no inference; "
                    "skill that uses it must look up by course_id directly).",
    )
    late_work_policy: str = Field(
        ..., min_length=20,
        description="The full policy text used in auto-replies.",
    )
    policy_last_verified: date = Field(
        ..., description="ISO date operator confirmed policy is current.",
    )
    current_term: str = Field(..., min_length=1, max_length=80)
    term_start: date
    term_end: date
    from_address: str = Field(
        ..., min_length=5,
        description="Send-from address for auto-replies (operator's "
                    "official course email, not personal).",
    )

    @field_validator("identifiers", mode="after")
    @classmethod
    def _identifiers_clean(cls, v: list[str]) -> list[str]:
        # Strip whitespace + drop empty strings. Empty list is allowed —
        # course-agnostic profiles legitimately don't need keyword
        # identifiers (the calling skill looks up the course by id).
        return [s.strip() for s in v if s.strip()]

    @field_validator("from_address", mode="after")
    @classmethod
    def _from_has_at(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError(f"from_address must look like an email: {v!r}")
        return v


def _max_age_days() -> int:
    """Operator-tunable staleness window for course policies."""
    try:
        from app.shared.runtime_tunables import get as _t
        return int(_t("course_policy_max_age_days"))
    except Exception:
        return 180


@lru_cache(maxsize=1)
def _load() -> dict[str, Course]:
    if not COURSES_PATH.exists():
        return {}
    raw = yaml.safe_load(COURSES_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    courses_block = raw.get("courses", []) or []
    out: dict[str, Course] = {}
    for entry in courses_block:
        course = Course.model_validate(entry)
        if course.course_id in out:
            raise ValueError(
                f"Duplicate course_id in courses.yaml: {course.course_id!r}"
            )
        out[course.course_id] = course
    return out


def reload() -> None:
    """Force re-read; mostly for tests."""
    _load.cache_clear()


def all_courses() -> dict[str, Course]:
    """Snapshot of every course in the registry."""
    return dict(_load())


def get_course_by_id(course_id: str) -> Optional[Course]:
    return _load().get(course_id)


def is_active_today(course: Course, today: Optional[date] = None) -> bool:
    """True if today falls within course.term_start … course.term_end."""
    today = today or datetime.now(UTC).date()
    return course.term_start <= today <= course.term_end


def is_policy_stale(course: Course, today: Optional[date] = None) -> bool:
    """True if policy_last_verified is older than the tunable window."""
    today = today or datetime.now(UTC).date()
    age = (today - course.policy_last_verified).days
    return age > _max_age_days()


def infer_course_from_text(
    text: str, *, only_active: bool = True,
) -> list[Course]:
    """Return courses whose identifiers appear in `text` (case-insensitive).

    Returns a LIST — caller decides how to handle 0 / 1 / many matches.
    Per principle #6 the cornell-delay-triage runner escalates on
    anything other than exactly-one match.
    """
    if not text:
        return []
    needle = text.lower()
    today = datetime.now(UTC).date()
    matches: list[Course] = []
    for course in _load().values():
        if only_active and not is_active_today(course, today):
            continue
        if any(ident.lower() in needle for ident in course.identifiers):
            matches.append(course)
    return matches
