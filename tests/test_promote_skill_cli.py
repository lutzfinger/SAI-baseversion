"""Tests for ``scripts/promote_skill.py`` — the CLI that validates,
integrity-stamps, and (optionally) moves a skill into place.

Per docs/design_live_public_versioning.md sections D + E. The CLI is
the only path that writes ``.skill-content-sha256`` for a freshly
promoted skill.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.skills.integrity import INTEGRITY_FILENAME, compute_skill_sha256
from scripts.promote_skill import main as promote_main


def _minimal_skill_files(skill_dir: Path, *, workflow_id: str = "test-skill") -> None:
    """Stage the smallest valid SAI skill: manifest + 3 required eval files."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1",
        "identity": {
            "workflow_id": workflow_id,
            "version": "0.1.0",
            "owner": "tester",
            "description": "test skill for promote_skill cli tests",
        },
        "trigger": {"kind": "manual", "config": {}},
        "cascade": [
            {"tier_id": "rules", "kind": "rules", "confidence_threshold": 0.85},
        ],
        "tools": [],
        "eval": {
            "datasets": [
                {"kind": "canaries", "path": "canaries.jsonl", "min_count": 1},
                {"kind": "edge_cases", "path": "edge_cases.jsonl", "min_count": 5},
                {"kind": "workflow", "path": "workflow_regression.jsonl", "min_count": 5},
            ],
        },
        "outputs": [
            {"name": "result", "side_effect": "none", "requires_approval": False},
        ],
    }
    (skill_dir / "skill.yaml").write_text(yaml.safe_dump(manifest))
    (skill_dir / "canaries.jsonl").write_text(
        "\n".join(f'{{"i":{i}}}' for i in range(1)) + "\n"
    )
    (skill_dir / "edge_cases.jsonl").write_text(
        "\n".join(f'{{"i":{i}}}' for i in range(5)) + "\n"
    )
    (skill_dir / "workflow_regression.jsonl").write_text(
        "\n".join(f'{{"i":{i}}}' for i in range(5)) + "\n"
    )


# ─── happy paths ──────────────────────────────────────────────────────


class TestPromoteHappyPath:
    def test_promote_moves_directory_and_stamps_hash(self, tmp_path):
        incoming = tmp_path / "incoming" / "draft-001"
        target = tmp_path / "skills" / "test-skill"
        _minimal_skill_files(incoming)

        rc = promote_main([
            "--incoming-dir", str(incoming),
            "--target-dir", str(target),
        ])
        assert rc == 0
        assert not incoming.exists(), "incoming dir should be moved away"
        assert target.exists(), "target dir should now exist"
        assert (target / INTEGRITY_FILENAME).exists(), \
            "promote should write the integrity hash"

        # Recorded hash must match a fresh re-computation.
        recorded = (target / INTEGRITY_FILENAME).read_text().strip()
        assert recorded == compute_skill_sha256(target)

    def test_in_place_restamps_without_moving(self, tmp_path):
        skill = tmp_path / "skills" / "in-place-skill"
        _minimal_skill_files(skill)

        rc = promote_main([
            "--in-place",
            "--incoming-dir", str(skill),
            "--target-dir", str(skill),
        ])
        assert rc == 0
        assert skill.exists()
        assert (skill / INTEGRITY_FILENAME).exists()

    def test_in_place_re_stamp_after_edit_picks_up_change(self, tmp_path):
        """Edit a file → re-stamp → recorded hash matches new content."""
        skill = tmp_path / "skills" / "edit-me"
        _minimal_skill_files(skill)

        rc1 = promote_main([
            "--in-place",
            "--incoming-dir", str(skill),
            "--target-dir", str(skill),
        ])
        assert rc1 == 0
        first_hash = (skill / INTEGRITY_FILENAME).read_text().strip()

        # Operator edits a runner — content changes, so should the hash.
        (skill / "canaries.jsonl").write_text('{"i":99}\n')

        rc2 = promote_main([
            "--in-place",
            "--incoming-dir", str(skill),
            "--target-dir", str(skill),
        ])
        assert rc2 == 0
        second_hash = (skill / INTEGRITY_FILENAME).read_text().strip()
        assert second_hash != first_hash


# ─── failure paths ────────────────────────────────────────────────────


class TestPromoteFailures:
    def test_missing_incoming_dir_returns_2(self, tmp_path):
        rc = promote_main([
            "--incoming-dir", str(tmp_path / "does_not_exist"),
            "--target-dir", str(tmp_path / "target"),
        ])
        assert rc == 2

    def test_invalid_manifest_returns_3(self, tmp_path):
        """A skill that fails schema validation must NOT be stamped or moved."""
        incoming = tmp_path / "incoming" / "broken"
        incoming.mkdir(parents=True)
        # Invalid manifest: missing required identity fields.
        (incoming / "skill.yaml").write_text("identity:\n  workflow_id: x\n")
        target = tmp_path / "skills" / "target"

        rc = promote_main([
            "--incoming-dir", str(incoming),
            "--target-dir", str(target),
        ])
        assert rc == 3
        # CRUCIAL: no integrity file written, no move performed —
        # validation fails CLOSED before any side effect.
        assert not (incoming / INTEGRITY_FILENAME).exists()
        assert not target.exists()

    def test_target_dir_already_exists_returns_4(self, tmp_path):
        incoming = tmp_path / "incoming" / "ok"
        target = tmp_path / "skills" / "occupied"
        _minimal_skill_files(incoming)
        target.mkdir(parents=True)
        (target / "preexisting.txt").write_text("don't clobber me")

        rc = promote_main([
            "--incoming-dir", str(incoming),
            "--target-dir", str(target),
        ])
        assert rc == 4
        # Preexisting file untouched.
        assert (target / "preexisting.txt").read_text() == "don't clobber me"
