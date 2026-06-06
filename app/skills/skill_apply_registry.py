"""Skill apply-on-approval registry.

Per `app/skills/proposal_intake.py` docstring: when an operator ✅s a
staged proposal in sai-eval, the slack bot loads the proposal YAML and
hands the body to the skill's `apply_approved_proposal(...)` function.

This module provides the *registry* mapping `workflow_id` → that
function, plus a `dispatch_approved_proposal(...)` helper that the
slack bot (or any other reaction surface) calls on ✅.

Adding a new skill is a one-line entry in `_REGISTRY` below. Each
skill ships its own `send_tool.py` with the function — the registry
just routes by workflow_id.

Per PRINCIPLES.md §6 (fail closed): if the workflow_id is not
registered, dispatch returns a clear `unregistered` result so the
bot can post an error to the operator. Never silently no-ops.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

LOGGER = logging.getLogger(__name__)


# Map workflow_id → (module path, function name). Module path is
# importable from the SAI root (cwd or sys.path[0]).
_REGISTRY: dict[str, tuple[str, str]] = {
    "student-participation-check": (
        "skills.student-participation-check.send_tool",
        "apply_approved_proposal",
    ),
    # Future entries:
    #   "cornell-delay-triage": ("skills.cornell-delay-triage.send_tool",
    #                            "apply_approved_proposal"),
}


@dataclass
class DispatchResult:
    ok: bool
    workflow_id: str
    proposal_id: str
    summary: str         # Slack-shaped one-liner ready to post in the bot
    raw_result: Any = None


def list_registered_workflows() -> list[str]:
    """Return sorted list of workflow_ids the registry can dispatch to.

    Useful for the slack bot at startup to log what's wired and for
    `/sai skills` style introspection.
    """
    return sorted(_REGISTRY.keys())


def is_registered(workflow_id: str) -> bool:
    return workflow_id in _REGISTRY


def _resolve_send_fn(workflow_id: str) -> Optional[Callable[[dict[str, Any]], Any]]:
    """Import the send_tool module + return its apply_approved_proposal fn.

    Skills with hyphens in their directory names (e.g. ``student-participation-check``)
    can't be imported with the bare hyphen path. We use importlib.util to load
    the module from a file path.

    Why we register the module in sys.modules BEFORE exec_module:
      Python's @dataclass decorator resolves type annotations via
      sys.modules.get(cls.__module__).__dict__ at class-creation time.
      If the dataclass-defining module isn't in sys.modules yet (because
      we're still executing it), the lookup returns None and the
      decorator crashes with "NoneType has no attribute __dict__".
      Standard fix per Python docs: insert into sys.modules first.
    """
    import sys as _sys
    import importlib.util as _ilu
    if workflow_id not in _REGISTRY:
        return None
    module_path, fn_name = _REGISTRY[workflow_id]
    parts = module_path.split(".")
    sai_root = Path(__file__).resolve().parents[2]
    file_path = sai_root.joinpath(*parts).with_suffix(".py")
    if not file_path.exists():
        LOGGER.error("send_tool not found at %s for workflow %s", file_path, workflow_id)
        return None
    mod_name = f"_send_tool_{workflow_id.replace('-', '_')}"
    if mod_name in _sys.modules:
        mod = _sys.modules[mod_name]
    else:
        spec = _ilu.spec_from_file_location(mod_name, file_path)
        mod = _ilu.module_from_spec(spec)
        _sys.modules[mod_name] = mod          # ← needed for @dataclass
        spec.loader.exec_module(mod)
    return getattr(mod, fn_name, None)


def dispatch_approved_proposal(proposal_path: Path) -> DispatchResult:
    """Load a staged proposal and call the right skill's send_tool.

    Returns a `DispatchResult` regardless of outcome — the caller (slack
    bot) posts `result.summary` to the operator and is never expected
    to handle exceptions from skill code.

    Per #6 fail closed: unregistered workflow_id or missing send_tool
    returns ok=False with a clear summary.
    """
    if not proposal_path.exists():
        return DispatchResult(
            ok=False, workflow_id="(unknown)", proposal_id=proposal_path.stem,
            summary=f":x: Proposal file vanished: `{proposal_path.name}`",
        )

    try:
        body = yaml.safe_load(proposal_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return DispatchResult(
            ok=False, workflow_id="(unparseable)", proposal_id=proposal_path.stem,
            summary=f":x: Couldn't parse proposal: `{type(exc).__name__}: {exc}`",
        )

    workflow_id = str(body.get("workflow_id") or proposal_path.parent.name)
    proposal_id = str(body.get("thread_id") or proposal_path.stem)

    if not is_registered(workflow_id):
        return DispatchResult(
            ok=False, workflow_id=workflow_id, proposal_id=proposal_id,
            summary=(
                f":x: No apply handler registered for workflow `{workflow_id}`.\n"
                f"To add: edit `app/skills/skill_apply_registry.py` and add to `_REGISTRY`."
            ),
        )

    send_fn = _resolve_send_fn(workflow_id)
    if send_fn is None:
        return DispatchResult(
            ok=False, workflow_id=workflow_id, proposal_id=proposal_id,
            summary=f":x: Could not load send_tool for `{workflow_id}`.",
        )

    try:
        result = send_fn(body)
    except Exception as exc:
        LOGGER.exception("send_tool crashed for %s", workflow_id)
        return DispatchResult(
            ok=False, workflow_id=workflow_id, proposal_id=proposal_id,
            summary=f":x: `{workflow_id}` send_tool crashed: `{type(exc).__name__}: {exc}`",
        )

    # Convert the skill's result dataclass to a Slack-shaped summary line.
    # All send_tools return objects with a `reason` field at minimum;
    # most also have a `wrote_*` / `sent` boolean.
    success = (getattr(result, "wrote_sheet", False)
               or getattr(result, "sent", False)
               or getattr(result, "applied", False))
    reason = getattr(result, "reason", str(result))
    if success:
        a1 = getattr(result, "sheet_a1", None)
        summary = f":white_check_mark: `{workflow_id}` applied. _{reason}_"
        if a1:
            summary += f"  (range: `{a1}`)"
    else:
        summary = f":warning: `{workflow_id}` did NOT write. _{reason}_"

    return DispatchResult(
        ok=success, workflow_id=workflow_id, proposal_id=proposal_id,
        summary=summary, raw_result=result,
    )
