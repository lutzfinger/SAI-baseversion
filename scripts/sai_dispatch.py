#!/usr/bin/env python3
"""Simulate a Slack DM / email to SAI end-to-end.

Useful for testing the full pipeline WITHOUT touching the running slack
bot daemon. Same code path the bot would call once integrated:

  1. Parse the natural-language message via `app.skills.skill_run_parser`.
  2. Resolve relative dates / folder names (TODO — for now requires
     explicit folder + sheet + dates).
  3. Invoke the skill's cascade via its `runner.run(inputs)`.
  4. The cascade stages a YAML proposal at
     ~/.sai-runtime/eval/proposed/<workflow_id>/<thread_id>.yaml
  5. Print the proposal path for operator review.

Operator then either:
  * Reacts ✅ in slack → real bot dispatches to skill's send_tool, OR
  * Runs `python -m skills.<wf>.send_tool <path>` manually (this script's
    --approve flag), with the kill switch env var set.

Usage
-----
  python -m scripts.sai_dispatch \\
      --message "run student participation check for C-Suites May 2026 INSEAD, all sessions, https://docs.google.com/spreadsheets/d/..../edit" \\
      --transcripts /tmp/granola-out/ \\
      --thread-id "smoke-2026-05-20"

  # Or to approve and fire the side effect in one shot:
  python -m scripts.sai_dispatch \\
      --message "..." --transcripts /tmp/... --thread-id smoke-1 \\
      --approve

The --approve flag requires the per-skill kill-switch env var to be set
(e.g. SAI_STUDENT_PARTICIPATION_SEND_ENABLED=1).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

_SAI_ROOT = Path(__file__).resolve().parents[1]
if str(_SAI_ROOT) not in sys.path:
    sys.path.insert(0, str(_SAI_ROOT))

from app.skills.skill_run_parser import parse_skill_run  # noqa: E402
from app.skills.skill_apply_registry import (
    dispatch_approved_proposal,
    list_registered_workflows,
)  # noqa: E402


def _load_skill_runner(workflow_id: str):
    """Load `skills/<workflow_id>/runner.py` via importlib."""
    runner_path = _SAI_ROOT / "skills" / workflow_id / "runner.py"
    if not runner_path.exists():
        sys.exit(f"ERROR: runner not found at {runner_path}")
    spec = importlib.util.spec_from_file_location(
        f"_runner_{workflow_id.replace('-', '_')}", runner_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    p = argparse.ArgumentParser(
        description="Simulate a Slack DM/email to SAI; route to the right skill."
    )
    p.add_argument("--message", required=True,
                   help="The natural-language message body (as if from a DM).")
    p.add_argument("--transcripts", type=Path,
                   help="Directory of pre-fetched transcript JSON files "
                        "(produced by `granola-fetch`). Required for skills "
                        "that need them.")
    p.add_argument("--students-file", type=Path,
                   help="Optional local roster file (alternative to sheet read).")
    p.add_argument("--thread-id", default="dispatch-cli",
                   help="Identifier the proposal will be staged under.")
    p.add_argument("--worksheet", default=None,
                   help="Optional Google Sheet tab name (default: first tab).")
    p.add_argument("--name-column", default="A")
    p.add_argument("--approve", action="store_true",
                   help="After staging, immediately call the skill's send_tool "
                        "(requires the kill-switch env var to be set).")
    p.add_argument("--dry-run-skill", action="store_true",
                   help="Pass dry_run=True into the skill (skips its external "
                        "writes even before approval).")
    args = p.parse_args()

    print(f"━━━ registered workflows: {list_registered_workflows()} ━━━")

    invocation = parse_skill_run(args.message)
    if invocation is None:
        sys.exit("ERROR: message did not match any registered 'run <skill>' parser.")
    print(f"matched:   {invocation.workflow_id} (phrase: '{invocation.matched_phrase}')")
    print(f"parsed:    folder={invocation.inputs.get('folder')!r} "
          f"dates={invocation.inputs.get('date_range')!r} "
          f"sheet={invocation.inputs.get('sheet_url')[:60] if invocation.inputs.get('sheet_url') else None!r}")
    if invocation.error_reason:
        sys.exit(f"ERROR: parser flagged issues: {invocation.error_reason}")

    # Map parsed slots → skill-specific inputs.
    runner = _load_skill_runner(invocation.workflow_id)
    skill_inputs: dict = {
        "transcripts_dir": str(args.transcripts) if args.transcripts else None,
        "sheet_url": invocation.inputs.get("sheet_url"),
        "students_file": str(args.students_file) if args.students_file else None,
        "worksheet": args.worksheet,
        "name_column": args.name_column,
        "folder": invocation.inputs.get("folder"),
        "date_range": invocation.inputs.get("date_range"),
        "thread_id": args.thread_id,
        "logfile": str(Path.home() / "Claude-Logs/SAI/student-participation-check/log.csv"),
        "dry_run": args.dry_run_skill,
    }
    # `transcripts_dir` is OPTIONAL — when absent, the composed skill's
    # `granola_fetch` tier pulls live transcripts via the Granola connector
    # (using GRANOLA_OP_REF or env to source the API key). When `transcripts_dir`
    # IS supplied, the skill uses the pre-fetched files (dev mode / back-compat).
    if skill_inputs.get("transcripts_dir") is None:
        # Remove the None so the cascade's `granola_fetch` tier triggers the
        # live-fetch path instead of validating an empty string.
        skill_inputs.pop("transcripts_dir", None)

    print()
    print("━━━ invoking skill cascade ━━━")
    result = runner.run(skill_inputs)
    print(f"final_verdict: {result.final_verdict}")
    print(f"final_reason:  {result.final_reason}")
    print(f"proposal_path: {result.proposal_path}")
    print()
    print("audit_log:")
    for a in result.audit_log:
        print(f"  - {a['tier']:<32} {a['kind']:<18} {a['reason'][:70]}")

    if result.final_verdict != "ready_to_propose" or not result.proposal_path:
        return 1 if result.final_verdict != "ready_to_propose" else 0

    if args.approve:
        print()
        print("━━━ --approve: dispatching to send_tool ━━━")
        dispatch = dispatch_approved_proposal(Path(result.proposal_path))
        print(f"ok:        {dispatch.ok}")
        print(f"summary:   {dispatch.summary}")
        return 0 if dispatch.ok else 1

    print()
    print("To approve manually:")
    print(f"  React ✅ on the staged proposal in #sai-eval, OR")
    print(f"  Set the kill switch + run:")
    print(f"    SAI_STUDENT_PARTICIPATION_SEND_ENABLED=1 \\")
    print(f"      python -m scripts.sai_dispatch --approve --message '...' "
          f"--transcripts ... --thread-id {args.thread_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
