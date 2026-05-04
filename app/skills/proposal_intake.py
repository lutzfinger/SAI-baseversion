"""Proposal-intake primitive (per #33a — framework, not skill).

When a skill's cascade returns ``ready_to_propose``, the framework
stages a YAML proposal at:

    ~/.sai-runtime/eval/proposed/<workflow_id>/<thread_id>.yaml

This module owns the inverse: reading those proposals back out so a
slack handler (or any other surface) can present them to the
operator for ✅/❌. Pure functions; no side effects.

Skills DO NOT import slack_bot. The slack_bot calls
``scan_pending_proposals(...)`` on a periodic timer and posts new
ones; on reaction it calls ``load_proposal(path)`` and hands the
draft to the skill's ``apply_approved_proposal(...)`` function.

Lookup keys:
  * directory listing for new-proposal sweep
  * filesystem path for direct apply
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict

LOGGER = logging.getLogger(__name__)


def proposed_root() -> Path:
    """Where staged proposals live in the merged runtime."""
    return Path.home() / ".sai-runtime" / "eval" / "proposed"


class StagedProposal(BaseModel):
    """One on-disk proposal awaiting operator approval.

    Per #6a the wrapper is strict (extra="forbid"). The `body` dict
    carries skill-specific YAML content; each skill validates its own
    body shape before consuming via its `apply_approved_proposal`.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    thread_id: str
    path: Path
    body: dict[str, Any]
    age_seconds: float

    def summary_text(self) -> str:
        """Short text the slack handler can post as the message body."""
        course = self.body.get("course_display_name") or self.body.get("course_id") or "(unknown course)"
        from_addr = self.body.get("from") or "(unknown sender)"
        ta_count = len(self.body.get("ta_emails") or [])
        return (
            f":envelope: e1 {self.workflow_id} proposal — *{course}*\n"
            f"From: `{from_addr}`\n"
            f"CC ({ta_count} TA{'s' if ta_count != 1 else ''})\n"
            f"React :white_check_mark: to send + label + archive, "
            f":x: to discard."
        )


def scan_pending_proposals(
    *,
    workflow_ids: Optional[list[str]] = None,
    root: Optional[Path] = None,
) -> list[StagedProposal]:
    """Return every YAML proposal currently on disk.

    `workflow_ids` filter: if provided, only these workflows are scanned.
    None = scan all workflow_id subdirectories.
    """

    base = root or proposed_root()
    if not base.exists():
        return []
    out: list[StagedProposal] = []
    now = datetime.now(UTC)
    target_dirs: list[Path]
    if workflow_ids is None:
        target_dirs = [d for d in base.iterdir() if d.is_dir()]
    else:
        target_dirs = [base / wf for wf in workflow_ids if (base / wf).is_dir()]
    for wf_dir in target_dirs:
        for path in wf_dir.glob("*.yaml"):
            try:
                body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                LOGGER.warning(
                    "skipping unparseable proposal %s: %s", path, exc,
                )
                continue
            if not isinstance(body, dict):
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            out.append(StagedProposal(
                workflow_id=str(body.get("workflow_id") or wf_dir.name),
                thread_id=str(body.get("thread_id") or path.stem),
                path=path,
                body=body,
                age_seconds=(now - mtime).total_seconds(),
            ))
    return out


def load_proposal(path: Path) -> Optional[StagedProposal]:
    """Load one proposal by filesystem path. None if missing/malformed."""

    if not path.exists():
        return None
    try:
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        LOGGER.warning("could not load %s: %s", path, exc)
        return None
    if not isinstance(body, dict):
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return StagedProposal(
        workflow_id=str(body.get("workflow_id") or path.parent.name),
        thread_id=str(body.get("thread_id") or path.stem),
        path=path,
        body=body,
        age_seconds=(datetime.now(UTC) - mtime).total_seconds(),
    )


def discard_proposal(path: Path) -> bool:
    """Remove the proposal file (operator ❌ path). Returns True on success."""

    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        LOGGER.warning("could not discard %s: %s", path, exc)
        return False
