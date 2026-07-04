"""Canary for the .claude danger guard.

The guard (`.claude/hooks/danger_guard.sh`) blocks a small denylist of destructive
commands (`git reset --hard`, `git push --force`/`-f`, `sudo`) and allows
everything else, including on an unparseable payload (denylist fails open by
design). This test is the standing regression guard for that behavior.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / ".claude" / "hooks" / "danger_guard.sh"

BLOCKED = [
    "git reset --hard origin/main",
    "git reset --hard",
    "git push --force",
    "git push origin main --force",
    "git push -f",
    "sudo rm -rf /x",
    "cd repo && git reset --hard",
    "/usr/bin/git reset --hard",
    "git -c x=y reset --hard",
]

ALLOWED = [
    "git commit -m 'x'",
    "git push",
    "git push origin main",
    "git reset --soft HEAD~1",
    "git reset HEAD file",
    "git push --force-with-lease",
    "git status",
    "ls -la",
    "echo hard reset done",
    "echo pseudocode",
]


def _run(command: str, *, raw_stdin: str | None = None) -> int:
    stdin = raw_stdin if raw_stdin is not None else json.dumps(
        {"tool_input": {"command": command}}
    )
    proc = subprocess.run(
        ["bash", str(GUARD)], input=stdin, text=True, capture_output=True,
        env={**os.environ},
    )
    return proc.returncode


def test_guard_exists_and_executable():
    assert GUARD.exists() and os.access(GUARD, os.X_OK)


def test_blocks_destructive_commands():
    for cmd in BLOCKED:
        assert _run(cmd) == 2, f"should have blocked: {cmd}"


def test_allows_safe_commands():
    for cmd in ALLOWED:
        assert _run(cmd) == 0, f"should have allowed: {cmd}"


def test_fails_open_on_unparseable_payload():
    # denylist semantics: cannot parse -> allow, never block everything.
    assert _run("unused", raw_stdin="not json at all") == 0
    assert _run("unused", raw_stdin='{"tool_input":{}}') == 0
