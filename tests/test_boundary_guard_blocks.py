"""Canary for the .claude PreToolUse boundary guard.

The guard (`.claude/hooks/boundary_guard.sh`) blocks `git commit` / `git push`
when the boundary linter reports a problem, so an agent cannot ship private data
even without the git pre-commit hook installed. This test is the standing
regression guard for that behavior - if a future edit weakens the hook, CI fails.

The block / fail-closed paths use a STUB `scripts/boundary_check.py` (hermetic;
the real linter is exercised by test_boundary_check_private_terms.py and
tests/runtime/test_boundary_check.py). One smoke test runs the guard against the
REAL repo to prove it wires to the real linter on a clean tree.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / ".claude" / "hooks" / "boundary_guard.sh"


def _run(project_dir: Path, command: str, *, raw_stdin: str | None = None) -> int:
    stdin = raw_stdin if raw_stdin is not None else json.dumps(
        {"tool_input": {"command": command}}
    )
    proc = subprocess.run(
        ["bash", str(GUARD)],
        input=stdin,
        text=True,
        capture_output=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(project_dir)},
    )
    return proc.returncode


def _project_with_stub(tmp_path: Path, exit_code: int) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "boundary_check.py").write_text(
        f"import sys\nprint('stub linter')\nsys.exit({exit_code})\n"
    )
    return tmp_path


def test_guard_exists_and_executable():
    assert GUARD.exists(), f"guard missing at {GUARD}"
    assert os.access(GUARD, os.X_OK), "guard is not executable"


def test_blocks_commit_on_violation(tmp_path):
    project = _project_with_stub(tmp_path, exit_code=1)
    assert _run(project, "git commit -m 'x'") == 2


def test_blocks_push_on_violation(tmp_path):
    project = _project_with_stub(tmp_path, exit_code=1)
    assert _run(project, "git push origin main") == 2


def test_allows_clean_commit(tmp_path):
    project = _project_with_stub(tmp_path, exit_code=0)
    assert _run(project, "git commit -m 'x'") == 0


def test_allows_non_git_command_without_scanning(tmp_path):
    # stub would exit 1 if invoked, but a non-git command must not be scanned.
    project = _project_with_stub(tmp_path, exit_code=1)
    assert _run(project, "ls -la") == 0


def test_no_false_positive_on_substring(tmp_path):
    # "digit committee" contains the literal substring "git commit" but is not a
    # git command; word-boundary detection must NOT scan/block it (cross-vendor
    # review finding). Stub exits 1, so a false match would surface as exit 2.
    project = _project_with_stub(tmp_path, exit_code=1)
    assert _run(project, "echo digit committee push") == 0


def test_blocks_git_with_global_option(tmp_path):
    # `git -c k=v commit` and `git -C dir commit` are common non-obfuscated forms
    # (cross-vendor review finding) and must still be caught.
    project = _project_with_stub(tmp_path, exit_code=1)
    assert _run(project, "git -c user.name=x commit -m y") == 2
    assert _run(project, "git -C /repo commit") == 2


def test_blocks_path_prefixed_git(tmp_path):
    project = _project_with_stub(tmp_path, exit_code=1)
    assert _run(project, "/usr/bin/git commit -m x") == 2


def test_allows_readonly_git(tmp_path):
    # read-only git commands must not be scanned (no friction); stub exits 1.
    project = _project_with_stub(tmp_path, exit_code=1)
    assert _run(project, "git status") == 0
    assert _run(project, "git diff HEAD") == 0


def test_fail_closed_when_linter_missing(tmp_path):
    # no scripts/boundary_check.py -> block, never silently allow.
    assert _run(tmp_path, "git commit -m 'x'") == 2


def test_blocks_broadened_writer_subcommand(tmp_path):
    # merge/rebase/etc. also record history and must be guarded.
    project = _project_with_stub(tmp_path, exit_code=1)
    assert _run(project, "git merge feature-branch") == 2


def test_fail_closed_on_malformed_stdin(tmp_path):
    project = _project_with_stub(tmp_path, exit_code=0)
    assert _run(project, "git commit -m 'x'", raw_stdin="not json at all") == 2


def test_fail_closed_on_missing_command(tmp_path):
    # a Bash payload with no command is an anomaly -> block, never allow.
    project = _project_with_stub(tmp_path, exit_code=0)
    assert _run(project, "unused", raw_stdin='{"tool_input":{}}') == 2


def test_fail_closed_on_nonstring_command(tmp_path):
    project = _project_with_stub(tmp_path, exit_code=0)
    assert _run(project, "unused", raw_stdin='{"tool_input":{"command":["git","commit"]}}') == 2


def test_real_repo_clean_tree_allows_commit():
    # Integration smoke: the guard wires to the REAL linter and a clean repo passes.
    assert _run(REPO_ROOT, "git commit -m 'x'") == 0
