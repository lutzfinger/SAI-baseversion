"""Preferences with version history — same data model for hard rules and soft preferences.

A Preference encodes a conditional decision for some task: "I prefer exit row when
flying", "always reply within 24h to customers", "never share financial details".
Strength is the runtime knob:

  HARD     — enforced as a constraint
  SOFT     — preferred but allow override on better cost / availability / etc.
  PROPOSED — extracted (e.g., from co-work) but not yet approved; ignored at runtime

Preferences are never edited silently. Every update is a new PreferenceVersion;
the current pointer flips only on explicit human approval (typically via a Slack
ask). Prior versions stay in `history` so we can roll back and see why a rule
changed.

Co-work sessions land here as PROPOSED preferences with `source=COWORK`. The
PreferenceRefiner LLM (separate component) lands its proposals as PROPOSED with
`source=INFERRED`. Either way, runtime ignores them until a human approves.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class PreferenceStrength(StrEnum):
    """How strongly the runtime should treat this preference."""

    HARD = "hard"          # enforced as a constraint; violation requires explicit override
    SOFT = "soft"          # preferred; runtime may override on better cost/availability
    PROPOSED = "proposed"  # not yet approved; runtime ignores


class PreferenceSource(StrEnum):
    """Where the preference (or this version of it) came from."""

    COWORK = "cowork"        # extracted from a real-time human+SAI session
    INFERRED = "inferred"    # PreferenceRefiner LLM proposed based on observed exceptions
    MANUAL = "manual"        # user authored directly (private overlay)


class PreferenceVersion(BaseModel):
    """One version of a preference. Versions are append-only; never edited."""

    model_config = ConfigDict(extra="forbid")

    rule_text: str                                 # YAML / DSL conditional, free-form
    strength: PreferenceStrength
    source: PreferenceSource
    proposed_at: datetime
    approved_at: datetime | None = None
    approved_by: str | None = None
    deprecated_at: datetime | None = None          # set when superseded by a new version
    approval_ask_id: str | None = None             # Slack ask that confirmed approval
    notes: str | None = None


class Preference(BaseModel):
    """A single preference for a task, with full version history."""

    model_config = ConfigDict(extra="forbid")

    preference_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    name: str                                      # short human-readable label
    description: str

    current: PreferenceVersion                     # latest version (may be PROPOSED)
    history: list[PreferenceVersion] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """True if this preference applies to runtime decisions."""

        return (
            self.current.strength in (PreferenceStrength.HARD, PreferenceStrength.SOFT)
            and self.current.approved_at is not None
            and self.current.deprecated_at is None
        )

    def propose_revision(self, new_version: PreferenceVersion) -> None:
        """Add a new proposed version; mark the previous as deprecated.

        Called when:
          - Co-work or refiner extracts a preference (initial PROPOSED)
          - PreferenceRefiner proposes refinement of an active preference
          - Human approves a proposed preference (PROPOSED → SOFT/HARD)

        Note: this only records the proposal. Promotion of a PROPOSED to
        runtime-active still requires `approved_at` to be set on the version.
        """

        if self.current.deprecated_at is None:
            self.current.deprecated_at = new_version.proposed_at
        self.history.append(self.current)
        self.current = new_version
