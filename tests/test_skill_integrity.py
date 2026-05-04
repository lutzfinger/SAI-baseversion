"""Tests for app/skills/integrity.py — skill content sha256 + drift detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.skills.integrity import (
    INTEGRITY_FILENAME,
    SkillIntegrityError,
    compute_skill_sha256,
    read_integrity_file,
    verify_skill_integrity,
    write_integrity_file,
)


def _make_skill(tmp_path: Path) -> Path:
    """Minimal valid-shaped skill directory."""
    skill = tmp_path / "test-skill"
    skill.mkdir()
    (skill / "skill.yaml").write_text("identity: {workflow_id: x}\n")
    (skill / "runner.py").write_text("# runner\n")
    (skill / "canaries.jsonl").write_text('{"a": 1}\n')
    (skill / "edge_cases.jsonl").write_text('{"b": 2}\n')
    (skill / "workflow_regression.jsonl").write_text('{"c": 3}\n')
    prompts = skill / "prompts"
    prompts.mkdir()
    (prompts / "system.md").write_text("# prompt\n")
    return skill


def test_compute_sha256_is_deterministic(tmp_path: Path):
    skill = _make_skill(tmp_path)
    a = compute_skill_sha256(skill)
    b = compute_skill_sha256(skill)
    assert a == b
    assert len(a) == 64  # SHA-256 hex


def test_compute_sha256_changes_when_file_changes(tmp_path: Path):
    skill = _make_skill(tmp_path)
    a = compute_skill_sha256(skill)
    (skill / "runner.py").write_text("# runner v2\n")
    b = compute_skill_sha256(skill)
    assert a != b


def test_compute_sha256_changes_when_prompt_changes(tmp_path: Path):
    skill = _make_skill(tmp_path)
    a = compute_skill_sha256(skill)
    (skill / "prompts" / "system.md").write_text("# prompt v2\n")
    b = compute_skill_sha256(skill)
    assert a != b


def test_readme_changes_do_not_affect_sha256(tmp_path: Path):
    """README is operator-facing docs; editing it shouldn't invalidate
    the skill (it's not part of what the framework executes)."""
    skill = _make_skill(tmp_path)
    a = compute_skill_sha256(skill)
    (skill / "README.md").write_text("# updated docs\n")
    b = compute_skill_sha256(skill)
    assert a == b


def test_pycache_does_not_affect_sha256(tmp_path: Path):
    skill = _make_skill(tmp_path)
    a = compute_skill_sha256(skill)
    pyc = skill / "__pycache__"
    pyc.mkdir()
    (pyc / "runner.cpython-312.pyc").write_text("compiled bytes")
    b = compute_skill_sha256(skill)
    assert a == b


def test_bak_files_do_not_affect_sha256(tmp_path: Path):
    skill = _make_skill(tmp_path)
    a = compute_skill_sha256(skill)
    # Operator-created backup file
    (skill / "runner.py.bak").write_text("# old runner\n")
    b = compute_skill_sha256(skill)
    assert a == b


def test_write_integrity_file_round_trip(tmp_path: Path):
    skill = _make_skill(tmp_path)
    sha = write_integrity_file(skill)
    recorded = read_integrity_file(skill)
    assert recorded == sha
    assert (skill / INTEGRITY_FILENAME).exists()


def test_verify_strict_raises_when_no_recorded_hash(tmp_path: Path):
    skill = _make_skill(tmp_path)
    with pytest.raises(SkillIntegrityError, match="no .skill-content-sha256"):
        verify_skill_integrity(skill, strict=True)


def test_verify_lenient_returns_current_when_no_recorded_hash(tmp_path: Path):
    skill = _make_skill(tmp_path)
    sha = verify_skill_integrity(skill, strict=False)
    assert len(sha) == 64


def test_verify_strict_passes_when_unchanged(tmp_path: Path):
    skill = _make_skill(tmp_path)
    write_integrity_file(skill)
    sha = verify_skill_integrity(skill, strict=True)
    assert len(sha) == 64


def test_verify_strict_raises_when_runner_edited(tmp_path: Path):
    skill = _make_skill(tmp_path)
    write_integrity_file(skill)
    (skill / "runner.py").write_text("# tampered\n")
    with pytest.raises(SkillIntegrityError, match="integrity check FAILED"):
        verify_skill_integrity(skill, strict=True)


def test_verify_strict_does_not_raise_on_readme_edit(tmp_path: Path):
    """README is excluded from the hash, so editing it must not break
    integrity verification."""
    skill = _make_skill(tmp_path)
    write_integrity_file(skill)
    (skill / "README.md").write_text("# edited docs\n")
    verify_skill_integrity(skill, strict=True)  # no raise
