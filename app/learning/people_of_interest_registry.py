"""Registry loader for the monitored people-of-interest list."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field


class PersonOfInterest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: str
    display_name: str
    canonical_url: str
    organization: str | None = None
    aliases: list[str] = Field(default_factory=list)
    notes: str | None = None


class PeopleOfInterestRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1"
    description: str | None = None
    people: list[PersonOfInterest] = Field(default_factory=list)


def load_people_of_interest_registry(path: Path) -> PeopleOfInterestRegistry:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"People-of-interest registry at {path} must be a YAML object")
    return PeopleOfInterestRegistry.model_validate(payload)
