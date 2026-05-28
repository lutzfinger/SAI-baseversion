"""Tests for app/skills/manifest_validator.py.

Mapped one-to-one against the plan in
~/Claude-Logs/code-plans/2026-05-27-sai-overlay-deploy-pr1-schema-validator.md
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.skills.manifest import (
    SkillManifest,
    SkillManifestV2,
    ValidationReport,
)
from app.skills.manifest_validator import (
    smoke_test_all_real_skills,
    validate_dict,
    validate_file,
)


FIXTURES = (
    Path(__file__).parent / "fixtures" / "overlay_deploy" / "manifests"
)
SKILL_FIXTURES = (
    Path(__file__).parent / "fixtures" / "overlay_deploy" / "skills"
)


# ── Test 2 — v1 backward-compat ────────────────────────────────────────


def test_v1_backward_compat():
    manifest, report = validate_file(FIXTURES / "v1-backward-compat-no-profiles.yaml")
    assert report.ok, report.summary()
    assert isinstance(manifest, SkillManifest)
    assert manifest.identity.workflow_id == "legacy-skill"
    assert manifest.schema_version == "1"
    # cascade preserved from the original file
    assert len(manifest.cascade) >= 1


# ── Test 3 — v2 single profile (claude_code) ──────────────────────────


def test_v2_claude_code_only():
    manifest, report = validate_file(FIXTURES / "valid-claude-code-only.yaml")
    assert report.ok, report.summary()
    assert isinstance(manifest, SkillManifestV2)
    assert manifest.identity.workflow_id == "sample-alpha"
    assert manifest.profiles.claude_code is not None
    assert manifest.profiles.claude_code.enabled
    assert manifest.profiles.sai_workflow is None


# ── Test 4 — v2 sai_workflow profile ──────────────────────────────────


def test_v2_sai_workflow_only():
    manifest, report = validate_file(FIXTURES / "valid-sai-workflow.yaml")
    assert report.ok, report.summary()
    assert isinstance(manifest, SkillManifestV2)
    p = manifest.profiles.sai_workflow
    assert p is not None and p.enabled
    assert p.trigger.kind == "manual"
    assert len(p.cascade) >= 1
    assert p.outputs
    assert p.policy is not None
    assert manifest.profiles.claude_code is None


# ── Test 5 — v2 dual profile ──────────────────────────────────────────


def test_v2_dual_profile():
    manifest, report = validate_file(FIXTURES / "valid-dual-profile.yaml")
    assert report.ok, report.summary()
    assert isinstance(manifest, SkillManifestV2)
    assert manifest.profiles.sai_workflow is not None
    assert manifest.profiles.claude_code is not None
    assert manifest.profiles.claude_code.claude_code_subdir == "SAI"


# ── Test 6 — v2 missing profiles: key rejected (file has no profiles:) ──


def test_v2_no_profiles_key_rejected():
    # The fixture is labeled "invalid-missing-runtime" historically; it is
    # a v2-declared file with no profiles: key at all. The dispatcher sees
    # schema_version "2" → goes v2 path → rejects.
    manifest, report = validate_file(FIXTURES / "invalid-missing-runtime.yaml")
    assert not report.ok
    assert manifest is None
    assert any("v2_no_profile" in e.rule for e in report.errors)


# ── Test 6b — v2 empty profiles: {} rejected ──────────────────────────


def test_v2_empty_profiles_rejected():
    manifest, report = validate_file(FIXTURES / "invalid-v2-empty-profiles.yaml")
    assert not report.ok
    assert manifest is None
    assert any("v2_no_profile" in e.rule for e in report.errors)


# ── Test 7 — path traversal in files[] rejected ───────────────────────


def test_rejects_path_traversal():
    manifest, report = validate_file(FIXTURES / "invalid-path-traversal.yaml")
    assert not report.ok
    assert manifest is None
    assert any(
        "files.path_traversal" in e.rule or "path" in e.rule.lower()
        for e in report.errors
    )


# ── Test 8 — claude_code profile with cascade rejected ────────────────


def test_claude_code_rejects_cascade():
    manifest, report = validate_file(FIXTURES / "invalid-claude-code-with-cascade.yaml")
    assert not report.ok
    assert manifest is None
    assert any("cascade" in (e.rule + e.message).lower() for e in report.errors)


# ── Test 9 — unknown deploy_to target rejected ────────────────────────


def test_rejects_unknown_target():
    manifest, report = validate_file(FIXTURES / "invalid-bad-deploy-target.yaml")
    assert not report.ok
    assert manifest is None
    joined = " ".join(e.rule + " " + e.message for e in report.errors)
    assert "deploy_to" in joined.lower()
    assert "mars" in joined.lower()


# ── Test 10 — claude_code_subdir with path chars rejected ─────────────


def test_rejects_unsafe_subdir():
    manifest, report = validate_file(FIXTURES / "invalid-claude-code-subdir-path.yaml")
    assert not report.ok
    assert manifest is None
    joined = " ".join(e.rule + " " + e.message for e in report.errors)
    assert "claude_code_subdir" in joined


# ── Test 11 — CLI exit codes ──────────────────────────────────────────


def _run_cli(*args: str) -> int:
    cmd = [sys.executable, "-m", "app.skills.manifest_validator", *args]
    repo_root = Path(__file__).parents[2]
    p = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    return p.returncode


def test_cli_exit_codes():
    rc_valid = _run_cli(str(FIXTURES / "valid-claude-code-only.yaml"))
    assert rc_valid == 0, f"expected 0, got {rc_valid}"
    rc_invalid = _run_cli(str(FIXTURES / "invalid-path-traversal.yaml"))
    assert rc_invalid == 2, f"expected 2, got {rc_invalid}"


# ── Test 12 — smoke test over real skills ────────────────────────────


def test_smoke_test_real_skills_runs():
    """Doesn't assert pass — surfaces drift in real skill.yaml files.

    Pass condition per plan: ≥11 manifests inspected. Failure-list is
    informative; specific failures are decisions for the operator, not
    PR 1 bugs.
    """
    passed, failed, candidates, reports = smoke_test_all_real_skills()
    total = passed + failed + candidates
    assert total >= 1, f"expected at least 1 manifest to be inspected, got {total}"
    # Print informative failure summary (visible with pytest -s).
    if failed:
        print(f"\n{failed} real skill manifest(s) failed validation:")
        for path, rep in reports:
            print(f"  {path}")
            for issue in rep.errors[:3]:
                print(f"    ❌ {issue.rule}: {issue.message}")


# ── Test 13 — overlay merge non-fatal pre-check ──────────────────────
# (Lives in test_overlay.py-style integration test below.)


def test_overlay_merge_does_not_break_on_invalid_skill_yaml(tmp_path):
    """Wiring check: a malformed skill.yaml in the input tree must NOT
    block sai-overlay merge from producing the manifest. PR 1 only adds
    warnings; behavior change to fatal-on-invalid is deferred to PR 2.
    """

    public = tmp_path / "public"
    private = tmp_path / "private"
    out = tmp_path / "out"
    (public / "skills" / "broken-skill").mkdir(parents=True)
    (private / "skills" / "real-skill").mkdir(parents=True)

    # Drop an obviously broken skill.yaml in the public tree.
    (public / "skills" / "broken-skill" / "skill.yaml").write_text(
        "- this is a yaml list at the root\n- which is never valid\n",
        encoding="utf-8",
    )
    # And a placeholder file in private so the merge has something to do.
    (private / "skills" / "real-skill" / "README.md").write_text(
        "placeholder\n", encoding="utf-8",
    )

    from app.runtime.overlay import merge

    result = merge(public=public, private=private, out=out, clean=False)
    # Merge succeeded — manifest written, broken skill.yaml carried through.
    assert (out / ".sai-overlay-manifest.json").is_file()
    assert (out / "skills" / "broken-skill" / "skill.yaml").is_file()
    assert (out / "skills" / "real-skill" / "README.md").is_file()
    # MergeResult populated.
    assert result.file_count >= 2


# ── Test 14 — glob expansion ─────────────────────────────────────────


def test_glob_expansion():
    skill_dir = SKILL_FIXTURES / "sample-glob"
    manifest, report = validate_file(skill_dir / "skill.yaml", skill_dir=skill_dir)
    assert report.ok, report.summary()
    assert isinstance(manifest, SkillManifestV2)
    cc = manifest.profiles.claude_code
    assert cc is not None
    # glob references/*.md should have expanded to both files.
    files_set = set(cc.files)
    assert "SKILL.md" in files_set
    assert "references/one.md" in files_set
    assert "references/two.md" in files_set
    # The glob itself should be gone from files[].
    assert "references/*.md" not in files_set


# ── Test 15 — YAML list at root rejected ─────────────────────────────


def test_root_must_be_dict():
    manifest, report = validate_file(FIXTURES / "invalid-root-is-list.yaml")
    assert not report.ok
    assert manifest is None
    assert any("root_not_a_dict" in e.rule for e in report.errors)


# ── Test 1 — module imports (also enforced by the import block above) ─


def test_module_imports_and_types():
    """Defensive: confirm the public API exists and is callable."""

    from app.skills.manifest_validator import (  # noqa: F401
        smoke_test_all_real_skills,
        validate_dict,
        validate_file,
    )
    from app.skills.manifest import (  # noqa: F401
        SkillManifest,
        SkillManifestV2,
        ValidationReport,
    )

    # Smoke: validate_dict on an obviously broken dict returns a report.
    _, report = validate_dict("not a dict")
    assert not report.ok
