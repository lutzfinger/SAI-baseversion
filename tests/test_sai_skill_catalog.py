"""Tests for ``scripts/sai_skill_catalog.py``.

The catalog renderer reads installed skills + emits a markdown summary
that the operator pastes into ``cowork_skill_creator_prompt.md`` so
Co-Work KNOWS what skills exist before designing a new one.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.sai_skill_catalog import (
    ANCHOR_BEGIN,
    ANCHOR_END,
    paste_into,
    render_catalog,
)


def _stage_skill(parent: Path, *, workflow_id: str, version: str = "0.1.0",
                 description: str = "test skill") -> Path:
    skill = parent / workflow_id
    skill.mkdir(parents=True)
    manifest = {
        "identity": {
            "workflow_id": workflow_id,
            "version": version,
            "owner": "tester",
            "description": description,
        },
        "trigger": {"kind": "manual"},
        "cascade": [
            {"tier_id": "rules", "kind": "rules"},
            {"tier_id": "human", "kind": "human"},
        ],
        "outputs": [{"name": "out_a", "side_effect": "none"}],
        "eval": {"datasets": [{"kind": "canaries"}, {"kind": "edge_cases"}]},
    }
    (skill / "skill.yaml").write_text(yaml.safe_dump(manifest))
    return skill


# ─── render_catalog ───────────────────────────────────────────────────


class TestRenderCatalog:
    def test_no_skills_dir_returns_placeholder(self, tmp_path):
        result = render_catalog(tmp_path / "absent")
        assert "No skills directory" in result

    def test_empty_skills_dir_returns_placeholder(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        result = render_catalog(skills)
        assert "No installed skills" in result

    def test_one_skill_rendered_with_metadata(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        _stage_skill(skills, workflow_id="my-workflow",
                     version="0.2.5", description="processes things")

        result = render_catalog(skills)
        assert "`my-workflow` (v0.2.5)" in result
        assert "processes things" in result
        assert "rules" in result   # cascade tier visible
        assert "human" in result
        assert "out_a" in result   # output name visible

    def test_incoming_directory_excluded(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        # `incoming/` is a staging area for Co-Work-emitted drafts;
        # they're not yet promoted skills and shouldn't appear.
        _stage_skill(skills / "incoming", workflow_id="draft-001")
        # Plus a real installed skill.
        _stage_skill(skills, workflow_id="real-skill")

        result = render_catalog(skills)
        assert "real-skill" in result
        assert "draft-001" not in result

    def test_bak_directories_excluded(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        # `.bak` directories are operator backups from previous promotes.
        _stage_skill(skills, workflow_id="old-skill.pre-v0.1.bak")
        _stage_skill(skills, workflow_id="current-skill")

        result = render_catalog(skills)
        assert "current-skill" in result
        assert "old-skill" not in result

    def test_directory_without_manifest_excluded(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        (skills / "not-a-skill").mkdir()  # no skill.yaml
        _stage_skill(skills, workflow_id="real-skill")

        result = render_catalog(skills)
        assert "real-skill" in result
        assert "not-a-skill" not in result

    def test_multiple_skills_sorted(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        _stage_skill(skills, workflow_id="b-second")
        _stage_skill(skills, workflow_id="a-first")
        _stage_skill(skills, workflow_id="c-third")

        result = render_catalog(skills)
        # Alphabetical order — index of each name in the output.
        a_idx = result.index("a-first")
        b_idx = result.index("b-second")
        c_idx = result.index("c-third")
        assert a_idx < b_idx < c_idx


# ─── paste_into ───────────────────────────────────────────────────────


class TestPasteInto:
    def test_anchor_block_replaced(self, tmp_path):
        prompt = tmp_path / "prompt.md"
        prompt.write_text(
            f"# Prompt\n\nintro\n\n{ANCHOR_BEGIN}\nold catalog\n{ANCHOR_END}\n\noutro\n"
        )
        new_catalog = "### `new-skill` (v1.0.0)\n\nfresh content\n"
        changed = paste_into(prompt, new_catalog)
        assert changed is True

        text = prompt.read_text()
        assert "old catalog" not in text
        assert "new-skill" in text
        assert "intro" in text
        assert "outro" in text

    def test_no_anchors_returns_false_and_warns(self, tmp_path, capsys):
        prompt = tmp_path / "prompt.md"
        prompt.write_text("# Prompt\n\nno anchors here\n")
        changed = paste_into(prompt, "anything\n")
        assert changed is False
        captured = capsys.readouterr()
        assert "Anchors not found" in captured.err

    def test_idempotent_when_catalog_unchanged(self, tmp_path):
        prompt = tmp_path / "prompt.md"
        catalog = "### `same` (v1.0.0)\n\nbody\n"
        prompt.write_text(
            f"# Prompt\n{ANCHOR_BEGIN}\n\n{catalog}\n{ANCHOR_END}\n"
        )
        # First call writes the same content.
        changed = paste_into(prompt, catalog)
        assert changed is False  # unchanged

    def test_multiline_old_block_fully_replaced(self, tmp_path):
        prompt = tmp_path / "prompt.md"
        prompt.write_text(
            f"intro\n{ANCHOR_BEGIN}\n"
            "### `old1` (v0.1.0)\n\nbody\n\n"
            "### `old2` (v0.2.0)\n\nbody\n"
            f"{ANCHOR_END}\noutro\n"
        )
        changed = paste_into(prompt, "### `new` (v1.0.0)\n\nbody\n")
        assert changed is True

        text = prompt.read_text()
        assert "old1" not in text
        assert "old2" not in text
        assert "new" in text
