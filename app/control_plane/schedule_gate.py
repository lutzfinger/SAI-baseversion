"""Small local-time slot gate for catch-up-friendly launchd pollers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ScheduleGateDecision:
    due: bool
    slot: str | None = None
    slot_key: str | None = None


def select_due_slot(
    *,
    state_path: Path,
    slots: list[str],
    now: datetime | None = None,
) -> ScheduleGateDecision:
    """Return the earliest local-time slot still due for today."""

    reference = now or datetime.now().astimezone()
    completed = _load_completed_slots(state_path)
    normalized_slots = sorted({_normalize_slot(slot) for slot in slots})
    local_date = reference.date().isoformat()
    current_minutes = reference.hour * 60 + reference.minute

    for slot in normalized_slots:
        hour, minute = _parse_slot(slot)
        if current_minutes < hour * 60 + minute:
            continue
        slot_key = f"{local_date}@{slot}"
        if slot_key in completed:
            continue
        return ScheduleGateDecision(due=True, slot=slot, slot_key=slot_key)
    return ScheduleGateDecision(due=False)


def mark_slot_completed(*, state_path: Path, slot_key: str, now: datetime | None = None) -> None:
    """Persist one completed slot key and prune old entries."""

    reference = now or datetime.now().astimezone()
    completed = _load_completed_slots(state_path)
    completed.add(slot_key.strip())
    _write_completed_slots(path=state_path, completed=completed, now=reference)


def _load_completed_slots(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    raw_completed = payload.get("completed_slot_keys", [])
    if not isinstance(raw_completed, list):
        return set()
    return {str(item).strip() for item in raw_completed if str(item).strip()}


def _write_completed_slots(*, path: Path, completed: set[str], now: datetime) -> None:
    cutoff = now.date().toordinal() - 7
    filtered = sorted(
        slot_key
        for slot_key in completed
        if (_slot_ordinal := _slot_key_ordinal(slot_key)) is None or _slot_ordinal >= cutoff
    )
    payload = {
        "completed_slot_keys": filtered,
        "updated_at": now.isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _slot_key_ordinal(slot_key: str) -> int | None:
    raw_date, _, _raw_slot = slot_key.partition("@")
    if not raw_date:
        return None
    try:
        return datetime.fromisoformat(raw_date).date().toordinal()
    except ValueError:
        return None


def _normalize_slot(slot: str) -> str:
    hour, minute = _parse_slot(slot)
    return f"{hour:02d}:{minute:02d}"


def _parse_slot(slot: str) -> tuple[int, int]:
    raw_hour, separator, raw_minute = slot.strip().partition(":")
    if separator != ":":
        raise ValueError(f"Invalid slot time {slot!r}; expected HH:MM.")
    try:
        hour = int(raw_hour)
        minute = int(raw_minute)
    except ValueError as error:
        raise ValueError(f"Invalid slot time {slot!r}; expected HH:MM.") from error
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid slot time {slot!r}; expected HH:MM.")
    return hour, minute
