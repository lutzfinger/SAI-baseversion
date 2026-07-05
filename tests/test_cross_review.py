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

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import cross_review  # noqa: E402  (safe: no top-level openai import)


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


def test_severity_parsing():
    assert cross_review._max_severity("HIGH: something -> fix") == 3
    assert cross_review._max_severity("CRITICAL: x\nMEDIUM: y") == 4
    assert cross_review._max_severity("It is sound; strongest residual risk is X.") == 0
    assert cross_review._max_severity("- **HIGH**: markered finding") == 3
    assert cross_review._max_severity("MEDIUM: only medium here") == 2
    # prose that merely starts with a severity word (no finding colon) must NOT gate
    assert cross_review._max_severity("High level overview of the change here.") == 0
    assert cross_review._max_severity("SEVERITY: HIGH: prefixed form -> fix") == 3


def test_severity_gate_thresholds():
    assert cross_review._gate_exit("HIGH: x", "none") == 0        # advisory, never gates
    assert cross_review._gate_exit("HIGH: x", "high") == 1        # HIGH >= high
    assert cross_review._gate_exit("MEDIUM: x", "high") == 0      # MEDIUM < high
    assert cross_review._gate_exit("HIGH: x", "critical") == 0    # HIGH < critical
    assert cross_review._gate_exit("CRITICAL: x", "critical") == 1
    assert cross_review._gate_exit("no findings at all", "high") == 0


def test_wrapper_gates_on_eval_and_review(tmp_path):
    wrapper = REPO_ROOT / "scripts" / "escape_with_review.sh"
    fake_py = tmp_path / "fakepy"  # stands in for the python that runs cross_review
    fake_py.write_text('#!/bin/sh\nexit ${MOCK_REVIEW_CODE:-0}\n')
    fake_py.chmod(0o755)

    def run(eval_cmd, review_code):
        env = {
            **os.environ, "PYTHON": str(fake_py), "EVAL_CMD": eval_cmd,
            "FILE": str(REPO_ROOT / "scripts" / "verify.sh"),
            "MOCK_REVIEW_CODE": str(review_code),
        }
        return subprocess.run(
            ["bash", str(wrapper)], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT)
        )

    assert run("true", 0).returncode == 0                     # eval green + review clean
    r = run("true", 1); assert r.returncode == 1 and "HIGH" in r.stderr        # review HIGH
    r = run("false", 0); assert r.returncode == 1 and "eval failed" in r.stderr  # eval fails
    r = run("true", 2); assert r.returncode == 1 and "tool error" in r.stderr    # review error
