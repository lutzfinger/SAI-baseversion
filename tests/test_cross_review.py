"""Canary for scripts/cross_review.py - the different-vendor review tool.

No network: every case here fails closed BEFORE any OpenAI call, so CI can run it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CR = REPO_ROOT / "scripts" / "cross_review.py"


def _run(args, *, env_extra=None, env_remove=()):
    env = {**os.environ}
    for key in env_remove:
        env.pop(key, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(CR), *args], capture_output=True, text=True, env=env
    )


def test_help_lists_options():
    r = _run(["--help"])
    assert r.returncode == 0
    assert "--file" in r.stdout and "--context" in r.stdout and "--focus" in r.stdout


def test_missing_key_fails_closed_no_secret_ref():
    r = _run(
        ["--file", str(REPO_ROOT / "scripts" / "verify.sh"), "--context", "a shell script"],
        env_remove=["OPENAI_API_KEY"],
    )
    assert r.returncode == 2
    assert "OPENAI_API_KEY" in r.stderr
    # the guidance must not leak a 1Password secret reference. Built from parts so
    # this test file itself stays boundary-clean (the linter flags the literal).
    secret_scheme = "op:" + "//"
    assert secret_scheme not in (r.stdout + r.stderr)


def test_unreadable_file_fails_closed():
    r = _run(["--file", "/no/such/file", "--context", "x"])
    assert r.returncode == 2


def test_refuses_private_file_before_network(tmp_path):
    leak = tmp_path / "leak.txt"
    # trigger built from parts so THIS source file stays boundary-clean; the
    # written file still contains the contiguous private term the linter flags.
    trigger = "someone@" + "web" + ".de"
    leak.write_text("contact " + trigger + "\n")
    # key present, but the boundary pre-flight must refuse BEFORE any network call
    r = _run(["--file", str(leak), "--context", "x"], env_extra={"OPENAI_API_KEY": "unused"})
    assert r.returncode == 2
    assert "REFUS" in r.stderr.upper()
