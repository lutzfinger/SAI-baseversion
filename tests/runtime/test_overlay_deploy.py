"""Tests for `sai-overlay deploy --target claude_code` (PR 2).

Mapped to ~/Claude-Logs/code-plans/2026-05-27-pr1a-schema-fits-reality-plus-pr2-deploy.md
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.runtime.overlay import (
    InputError,
    deploy_claude_code,
    plan_claude_code_deploy,
)


def _make_skill(runtime_tree: Path, skill_id: str = "demo-skill") -> Path:
    """Write a minimal valid v2 claude_code skill into a runtime tree."""
    sd = runtime_tree / "skills" / skill_id
    (sd / "profiles" / "claude_code").mkdir(parents=True)
    (sd / "skill.yaml").write_text(
        f"""schema_version: "2"
identity:
  workflow_id: {skill_id}
  version: "0.1.0"
  owner: tests
  description: "Hermetic deploy-test skill."
profiles:
  claude_code:
    enabled: true
    files: [SKILL.md]
    deploy_to: [claude_code]
    eval:
      datasets:
        - {{ kind: canaries, path: profiles/claude_code/canaries.jsonl, fail_mode: hard_fail, min_count: 1 }}
""",
        encoding="utf-8",
    )
    (sd / "SKILL.md").write_text("# demo-skill\n\nbody\n", encoding="utf-8")
    (sd / "profiles" / "claude_code" / "canaries.jsonl").write_text(
        '{"id":"c1","input":"x","expect":"x"}\n', encoding="utf-8"
    )
    return sd


def _make_tag_repo(repo: Path, tag: str) -> None:
    """Init a git repo with one commit and a (lightweight) tag."""
    repo.mkdir(parents=True, exist_ok=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"],
                   check=True, env={**env})
    subprocess.run(["git", "-C", str(repo), "tag", tag], check=True)


# ── Test 8 — dry-run writes nothing ──────────────────────────────────


def test_dry_run_writes_nothing(tmp_path):
    rt = tmp_path / "rt"
    _make_skill(rt)
    cc_root = tmp_path / "cc"
    plan = deploy_claude_code(
        skill_id="demo-skill", runtime_tree=rt, claude_code_root=cc_root,
        apply=False, approved_by=None, sai_repo=tmp_path / "repo",
    )
    assert plan.files == ["SKILL.md"]
    assert not cc_root.exists()  # nothing written


# ── Test 9 — apply without approval fails closed ─────────────────────


def test_apply_without_approval_fails_closed(tmp_path):
    rt = tmp_path / "rt"
    _make_skill(rt)
    cc_root = tmp_path / "cc"
    with pytest.raises(InputError) as ei:
        deploy_claude_code(
            skill_id="demo-skill", runtime_tree=rt, claude_code_root=cc_root,
            apply=True, approved_by=None, sai_repo=tmp_path / "repo",
        )
    assert "approved-by" in str(ei.value) or "approval" in str(ei.value).lower()
    assert not cc_root.exists()


# ── Test 9b — apply with nonexistent tag fails closed ────────────────


def test_apply_with_missing_tag_fails_closed(tmp_path):
    rt = tmp_path / "rt"
    _make_skill(rt)
    cc_root = tmp_path / "cc"
    repo = tmp_path / "repo"
    _make_tag_repo(repo, "some/other/tag")
    with pytest.raises(InputError) as ei:
        deploy_claude_code(
            skill_id="demo-skill", runtime_tree=rt, claude_code_root=cc_root,
            apply=True, approved_by="sync_skills/demo-skill/v0.1.0", sai_repo=repo,
        )
    assert "not found" in str(ei.value)
    assert not cc_root.exists()


# ── Test 10 — apply with valid tag writes + logs + filters private ───


def test_apply_writes_logs_and_filters_private(tmp_path):
    rt = tmp_path / "rt"
    sd = _make_skill(rt)
    cc_root = tmp_path / "cc"
    repo = tmp_path / "repo"
    tag = "sync_skills/demo-skill/v0.1.0"
    _make_tag_repo(repo, tag)

    deploy_claude_code(
        skill_id="demo-skill", runtime_tree=rt, claude_code_root=cc_root,
        apply=True, approved_by=tag, sai_repo=repo,
    )

    # SKILL.md written, hash matches source.
    dst = cc_root / "demo-skill" / "SKILL.md"
    assert dst.is_file()
    assert dst.read_text() == (sd / "SKILL.md").read_text()

    # Private eval NOT written (it isn't in files[], and the filter is belt+suspenders).
    assert not (cc_root / "demo-skill" / "profiles").exists()
    assert not (cc_root / "demo-skill" / "canaries.jsonl").exists()

    # Deploy log row appended.
    log = rt / ".sai-deploy-log.jsonl"
    assert log.is_file()
    rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["skill"] == "demo-skill"
    assert rows[0]["target"] == "claude_code"
    assert rows[0]["approved_by"] == tag
    assert rows[0]["files"][0]["relpath"] == "SKILL.md"
    assert rows[0]["result"] == "ok"


# ── Test 10b — idempotent re-apply overwrites cleanly ────────────────


def test_reapply_is_clean(tmp_path):
    rt = tmp_path / "rt"
    _make_skill(rt)
    cc_root = tmp_path / "cc"
    repo = tmp_path / "repo"
    tag = "sync_skills/demo-skill/v0.1.0"
    _make_tag_repo(repo, tag)
    for _ in range(2):
        deploy_claude_code(
            skill_id="demo-skill", runtime_tree=rt, claude_code_root=cc_root,
            apply=True, approved_by=tag, sai_repo=repo,
        )
    dst = cc_root / "demo-skill" / "SKILL.md"
    assert dst.is_file()
    # No leftover .tmp file from the atomic write.
    assert not (cc_root / "demo-skill" / "SKILL.md.tmp").exists()
    # Two deploy-log rows (one per apply).
    rows = [l for l in (rt / ".sai-deploy-log.jsonl").read_text().splitlines() if l.strip()]
    assert len(rows) == 2


# ── Test 12 — --require-signed rejects an unsigned tag ───────────────


def test_require_signed_rejects_unsigned(tmp_path):
    rt = tmp_path / "rt"
    _make_skill(rt)
    cc_root = tmp_path / "cc"
    repo = tmp_path / "repo"
    tag = "sync_skills/demo-skill/v0.1.0"
    _make_tag_repo(repo, tag)  # lightweight (unsigned) tag
    with pytest.raises(InputError) as ei:
        deploy_claude_code(
            skill_id="demo-skill", runtime_tree=rt, claude_code_root=cc_root,
            apply=True, approved_by=tag, sai_repo=repo, require_signed=True,
        )
    assert "signed" in str(ei.value).lower()
    assert not cc_root.exists()  # fail-closed, nothing written


# ── Test (extra) — unknown skill / no claude_code profile fails ──────


def test_unknown_skill_fails(tmp_path):
    rt = tmp_path / "rt"
    (rt / "skills").mkdir(parents=True)
    with pytest.raises(InputError):
        plan_claude_code_deploy(
            skill_id="nope", runtime_tree=rt, claude_code_root=tmp_path / "cc",
        )
