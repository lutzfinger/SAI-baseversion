#!/usr/bin/env python3
"""Bounded loop runner - the safety contract for an iterate-until-green loop.

Runs --attempt-cmd, then --escape-cmd, repeatedly, until the escape check passes
OR a hard cap trips. The runner itself NEVER sends anything to a remote and never
rewrites history: that guarantee is about THIS RUNNER. The --attempt-cmd you pass
must not send to a remote either (the loop iterates locally, it does not ship);
run it in a throwaway worktree and instruct the driver "edit only".

The agent-driver plugs into --attempt-cmd. Example (headless Claude Code):
  --attempt-cmd 'claude -p "<task>. Edit files only." --permission-mode acceptEdits'
Use acceptEdits (auto-approves edits, KEEPS PreToolUse hooks blocking); do NOT use
--dangerously-skip-permissions. Headless hooks still fire, so the danger_guard and
boundary_guard hooks remain load-bearing even mid-loop.

Both commands run non-interactively (stdin is /dev/null) and under a wall-clock
timeout equal to the remaining budget. Progress is fingerprinted by HEAD + working
tree, so a committing agent is NOT mistaken for stuck.

Exit codes:
  0  escape check passed (success)
  2  invalid arguments, OR the escape check could not be run (fail closed)
  3  iteration cap reached
  4  wall-clock cap reached
  5  stuck: N consecutive no-progress iterations (git worktrees only)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

STUCK_LIMIT = 3
_ESCAPE_UNRUNNABLE = 127  # shell "command not found" (best-effort; caps still backstop)


def _git(repo: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def _fingerprint(repo: str) -> tuple[str, str] | None:
    """(HEAD, porcelain status), or None if `repo` is not a git worktree.

    Includes HEAD so an agent that COMMITS progress (clean working tree, moved
    HEAD) is not falsely flagged as stuck."""
    status = _git(repo, "status", "--porcelain")
    if status is None:
        return None
    head = _git(repo, "rev-parse", "HEAD")  # None/"" before the first commit
    return (head or "", status)


def _run(cmd: str, cwd: str, timeout: float) -> tuple[int | None, bool]:
    """Run a shell command non-interactively under a timeout.

    Returns (exit_code, timed_out). exit_code is None on timeout."""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, timeout=timeout
        )
        return result.returncode, False
    except subprocess.TimeoutExpired:
        return None, True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bounded iterate-until-green loop runner.")
    ap.add_argument("--attempt-cmd", required=True, help="command that attempts the task")
    ap.add_argument("--escape-cmd", required=True, help="command; exit 0 means done")
    ap.add_argument("--max-iterations", type=int, default=10)
    ap.add_argument("--max-seconds", type=int, default=1800)
    ap.add_argument("--repo", default=".")
    args = ap.parse_args(argv)

    if args.max_iterations < 1 or args.max_seconds < 1:
        print("bounded_loop: --max-iterations and --max-seconds must be >= 1", file=sys.stderr)
        return 2

    repo = str(Path(args.repo).resolve())
    start = time.monotonic()

    def log(msg: str) -> None:
        print(f"[bounded_loop] {msg}", flush=True)

    def remaining() -> float:
        return args.max_seconds - (time.monotonic() - start)

    prev_fp = _fingerprint(repo)  # None -> not a git repo -> stuck detection off
    no_change = 0

    for i in range(1, args.max_iterations + 1):
        if remaining() <= 0:
            log(f"STOP time-cap after {i - 1} iterations ({args.max_seconds}s)")
            return 4
        log(f"iteration {i}/{args.max_iterations}: attempt")
        _, timed = _run(args.attempt_cmd, repo, remaining())  # attempt failure = failed iteration
        if timed:
            log("STOP time-cap (attempt exceeded remaining budget)")
            return 4

        if remaining() <= 0:
            log(f"STOP time-cap after {i} iterations ({args.max_seconds}s)")
            return 4
        code, timed = _run(args.escape_cmd, repo, remaining())
        if timed:
            log("STOP time-cap (escape check exceeded remaining budget)")
            return 4
        if code == _ESCAPE_UNRUNNABLE:
            log("STOP cannot evaluate escape check (command not found) - fail closed")
            return 2
        if code == 0:
            log(f"SUCCESS escape check passed at iteration {i}")
            return 0

        if prev_fp is not None:
            current = _fingerprint(repo)
            if current is not None:
                no_change = no_change + 1 if current == prev_fp else 0
                prev_fp = current
                if no_change >= STUCK_LIMIT:
                    log(f"STOP stuck: {STUCK_LIMIT} consecutive no-progress iterations")
                    return 5

    log(f"STOP iteration-cap after {args.max_iterations} iterations")
    return 3


if __name__ == "__main__":
    sys.exit(main())
