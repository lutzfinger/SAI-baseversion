"""Canary for scripts/bounded_loop.py - the bounded-loop runner.

No real agent, no network: every case uses mock shell commands, deterministic
and cheap, so CI can run it.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOOP = REPO_ROOT / "scripts" / "bounded_loop.py"


def _loop(args, *, env_extra=None):
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(LOOP), *args], capture_output=True, text=True, env=env
    )


def test_escapes_when_check_passes(tmp_path):
    r = _loop([
        "--repo", str(tmp_path),
        "--attempt-cmd", f"touch {tmp_path}/done",
        "--escape-cmd", f"test -f {tmp_path}/done",
        "--max-iterations", "5",
    ])
    assert r.returncode == 0
    assert "SUCCESS" in r.stdout


def test_iteration_cap_stop(tmp_path):
    r = _loop([
        "--repo", str(tmp_path),
        "--attempt-cmd", "true", "--escape-cmd", "false",
        "--max-iterations", "3",
    ])
    assert r.returncode == 3
    assert "iteration-cap" in r.stdout


def test_stuck_stop_in_git_repo(tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    r = _loop([
        "--repo", str(tmp_path),
        "--attempt-cmd", "true", "--escape-cmd", "false",
        "--max-iterations", "20",
    ])
    assert r.returncode == 5
    assert "stuck" in r.stdout


def test_time_cap_stop(tmp_path):
    # a long attempt under a short budget -> subprocess timeout -> time-cap (deterministic).
    r = _loop([
        "--repo", str(tmp_path),
        "--attempt-cmd", "sleep 5", "--escape-cmd", "false",
        "--max-iterations", "100", "--max-seconds", "1",
    ])
    assert r.returncode == 4
    assert "time-cap" in r.stdout


def test_rejects_bad_caps(tmp_path):
    for bad in (["--max-iterations", "0"], ["--max-seconds", "0"]):
        r = _loop(["--repo", str(tmp_path), "--attempt-cmd", "true",
                   "--escape-cmd", "false", *bad])
        assert r.returncode == 2, f"bad caps must be rejected: {bad}"


def test_not_stuck_when_head_advances(tmp_path):
    # an agent that COMMITS each iteration (clean tree, moved HEAD) is NOT stuck;
    # the loop should hit the iteration cap, not the stuck stop.
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    commit = (
        f'date >> {tmp_path}/f && git -C {tmp_path} add f && '
        f'git -C {tmp_path} -c user.email=a@b.c -c user.name=x commit -q -m step'
    )
    r = _loop([
        "--repo", str(tmp_path),
        "--attempt-cmd", commit, "--escape-cmd", "false",
        "--max-iterations", "4",
    ])
    assert r.returncode == 3, "committing progress must not be flagged as stuck"
    assert "iteration-cap" in r.stdout


def test_escape_unrunnable_fails_closed(tmp_path):
    r = _loop([
        "--repo", str(tmp_path),
        "--attempt-cmd", "true", "--escape-cmd", "/no/such/escape-binary",
        "--max-iterations", "5",
    ])
    assert r.returncode == 2
    assert "cannot evaluate" in r.stdout


def test_wiring_with_mock_claude_driver(tmp_path):
    # a fake `claude` on PATH that, on any args, appends to $TARGET (simulates an edit)
    target = tmp_path / "f"
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\necho fixed >> "$TARGET"\n')
    fake.chmod(0o755)
    r = _loop(
        [
            "--repo", str(tmp_path),
            "--attempt-cmd", 'claude -p "make progress"',
            "--escape-cmd", f'grep -q fixed "{target}"',
            "--max-iterations", "3",
        ],
        env_extra={"PATH": f"{tmp_path}:{os.environ['PATH']}", "TARGET": str(target)},
    )
    assert r.returncode == 0
    assert "SUCCESS" in r.stdout


def test_runner_source_never_pushes():
    src = LOOP.read_text()
    assert not re.search(r"git +push|--force|git +tag", src), (
        "the runner must contain no push/force/tag verb - it never ships"
    )
