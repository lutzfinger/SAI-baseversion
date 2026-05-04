"""Tests for the skill plug-in protocol — schema + loader + sample.

Coverage:

  * SkillManifest validates the sample skill cleanly
  * Hard-contract refusals: missing eval files, agent tier without
    tools, propose_only without two-phase commit, mutate_with_approval
    without policy gate, side-effect output without approval/human tier
  * Soft-contract warnings: cost cap > $1, daily cap > 1000,
    vendor-specific tool input names
  * Loader handles missing manifest, malformed YAML, schema fail
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from app.skills.loader import (
    SKILL_MANIFEST_FILENAME,
    discover_skills,
    load_skill_manifest,
    validate_skill_manifest,
)
from app.skills.manifest import (
    CanariesSpec,
    CascadeTier,
    EdgeCasesSpec,
    SkillEval,
    SkillIdentity,
    SkillManifest,
    SkillOutput,
    SkillPolicy,
    SkillTrigger,
    ToolDeclaration,
    WorkflowSpec,
)


SAMPLE_SKILL_DIR = (
    Path(__file__).parent.parent / "app" / "skills" / "sample_echo_skill"
)


def _minimal_manifest_dict() -> dict:
    """Smallest manifest that should validate against the schema."""

    return {
        "schema_version": "1",
        "identity": {
            "workflow_id": "test-skill",
            "version": "0.1.0",
            "owner": "tester",
            "description": "test skill for unit tests",
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


def _stage_skill(
    tmp_path: Path,
    *,
    manifest: dict | None = None,
    canary_rows: int = 5,
    edge_rows: int = 5,
    workflow_regression_rows: int = 5,
) -> Path:
    """Stage a temp skill dir + eval files."""

    skill_dir = tmp_path / "test_skill"
    skill_dir.mkdir()
    m = manifest or _minimal_manifest_dict()
    (skill_dir / SKILL_MANIFEST_FILENAME).write_text(yaml.safe_dump(m))
    (skill_dir / "canaries.jsonl").write_text(
        "\n".join(f'{{"i":{i}}}' for i in range(canary_rows)) + ("\n" if canary_rows else "")
    )
    (skill_dir / "edge_cases.jsonl").write_text(
        "\n".join(f'{{"i":{i}}}' for i in range(edge_rows)) + ("\n" if edge_rows else "")
    )
    (skill_dir / "workflow_regression.jsonl").write_text(
        "\n".join(f'{{"i":{i}}}' for i in range(workflow_regression_rows))
        + ("\n" if workflow_regression_rows else "")
    )
    return skill_dir


# ─── schema-level tests ───────────────────────────────────────────────


class TestSkillManifestSchema:
    def test_minimal_validates(self):
        m = SkillManifest.model_validate(_minimal_manifest_dict())
        assert m.identity.workflow_id == "test-skill"
        assert len(m.cascade) == 1
        assert m.policy.iteration_cap == 8  # default

    def test_extra_fields_rejected(self):
        d = _minimal_manifest_dict()
        d["unknown_field"] = "value"
        with pytest.raises(Exception):
            SkillManifest.model_validate(d)

    def test_cascade_must_be_non_empty(self):
        d = _minimal_manifest_dict()
        d["cascade"] = []
        with pytest.raises(Exception):
            SkillManifest.model_validate(d)

    def test_cascade_tier_ids_must_be_unique(self):
        d = _minimal_manifest_dict()
        d["cascade"] = [
            {"tier_id": "x", "kind": "rules", "confidence_threshold": 0.85},
            {"tier_id": "x", "kind": "cloud_llm", "confidence_threshold": 0.7},
        ]
        with pytest.raises(Exception, match="tier_ids must be unique"):
            SkillManifest.model_validate(d)

    def test_outputs_must_be_non_empty(self):
        d = _minimal_manifest_dict()
        d["outputs"] = []
        with pytest.raises(Exception):
            SkillManifest.model_validate(d)


# ─── hard-contract validation tests ───────────────────────────────────


class TestHardContract:
    def test_missing_canaries_file_rejected(self, tmp_path):
        skill = _stage_skill(tmp_path)
        (skill / "canaries.jsonl").unlink()
        m, report = load_skill_manifest(skill)
        assert m is not None  # schema validates
        assert not report.ok
        assert any("eval.canaries.missing" in e.rule for e in report.errors)

    def test_below_min_count_rejected(self, tmp_path):
        skill = _stage_skill(tmp_path, edge_rows=2)  # min default = 5
        m, report = load_skill_manifest(skill)
        assert not report.ok
        assert any("below_min_count" in e.rule for e in report.errors)

    def test_agent_tier_without_tools_rejected(self, tmp_path):
        d = _minimal_manifest_dict()
        d["cascade"].append({
            "tier_id": "agent", "kind": "agent",
            "confidence_threshold": 0.7,
        })
        # tools stays empty
        skill = _stage_skill(tmp_path, manifest=d)
        m, report = load_skill_manifest(skill)
        assert not report.ok
        assert any("agent_tier_requires_surface" in e.rule for e in report.errors)

    def test_propose_only_without_two_phase_commit_rejected(self, tmp_path):
        d = _minimal_manifest_dict()
        d["cascade"].append({
            "tier_id": "agent", "kind": "agent",
            "confidence_threshold": 0.7,
        })
        d["tools"] = [{
            "tool_id": "propose_thing",
            "rights": "propose_only",
            "blast_radius": "writes a yaml proposal",
        }]
        # Default policy.approval_required=False AND no output requires_approval
        skill = _stage_skill(tmp_path, manifest=d)
        m, report = load_skill_manifest(skill)
        assert not report.ok
        assert any("propose_only_needs_two_phase_commit" in e.rule for e in report.errors)

    def test_propose_only_with_approval_output_passes(self, tmp_path):
        d = _minimal_manifest_dict()
        d["cascade"].append({
            "tier_id": "agent", "kind": "agent",
            "confidence_threshold": 0.7,
        })
        d["tools"] = [{
            "tool_id": "propose_thing",
            "rights": "propose_only",
            "blast_radius": "writes a yaml proposal",
        }]
        d["outputs"] = [
            {"name": "proposed", "side_effect": "propose", "requires_approval": True},
        ]
        skill = _stage_skill(tmp_path, manifest=d)
        m, report = load_skill_manifest(skill)
        assert report.ok, report.summary()

    def test_mutate_with_approval_without_policy_rejected(self, tmp_path):
        d = _minimal_manifest_dict()
        d["cascade"].append({
            "tier_id": "agent", "kind": "agent",
            "confidence_threshold": 0.7,
        })
        d["tools"] = [{
            "tool_id": "delete_thing",
            "rights": "mutate_with_approval",
            "blast_radius": "deletes things",
        }]
        d["policy"] = {"approval_required": False}
        skill = _stage_skill(tmp_path, manifest=d)
        m, report = load_skill_manifest(skill)
        assert not report.ok
        assert any("mutate_requires_approval_policy" in e.rule for e in report.errors)

    def test_send_output_needs_approval_or_human_tier(self, tmp_path):
        d = _minimal_manifest_dict()
        d["outputs"] = [
            {"name": "reply", "side_effect": "send", "requires_approval": False},
        ]
        # No human tier in cascade either.
        skill = _stage_skill(tmp_path, manifest=d)
        m, report = load_skill_manifest(skill)
        assert not report.ok
        assert any("side_effect_needs_gate" in e.rule for e in report.errors)

    def test_send_output_with_human_tier_passes(self, tmp_path):
        d = _minimal_manifest_dict()
        d["cascade"].append({
            "tier_id": "human", "kind": "human", "confidence_threshold": 1.0,
        })
        d["outputs"] = [
            {"name": "reply", "side_effect": "send", "requires_approval": False},
        ]
        skill = _stage_skill(tmp_path, manifest=d)
        m, report = load_skill_manifest(skill)
        assert report.ok, report.summary()


# ─── soft-contract warnings ───────────────────────────────────────────


class TestSoftContract:
    def test_cost_cap_above_dollar_warns(self, tmp_path):
        d = _minimal_manifest_dict()
        d["policy"] = {"cost_cap_per_invocation_usd": 1.50}
        skill = _stage_skill(tmp_path, manifest=d)
        m, report = load_skill_manifest(skill)
        assert report.ok  # warnings don't block
        assert any("policy.cost_cap_high" in w.rule for w in report.warnings)

    def test_daily_cap_above_1000_warns(self, tmp_path):
        d = _minimal_manifest_dict()
        d["policy"] = {"daily_invocation_cap": 5000}
        skill = _stage_skill(tmp_path, manifest=d)
        m, report = load_skill_manifest(skill)
        assert report.ok
        assert any("policy.daily_cap_high" in w.rule for w in report.warnings)

    def test_vendor_specific_tool_input_warns(self, tmp_path):
        d = _minimal_manifest_dict()
        d["cascade"].append({
            "tier_id": "agent", "kind": "agent",
            "confidence_threshold": 0.7,
        })
        d["tools"] = [{
            "tool_id": "do_thing",
            "rights": "read_only",
            "blast_radius": "reads stuff",
            "inputs": {"openai_client_token": "vendor token"},
        }]
        skill = _stage_skill(tmp_path, manifest=d)
        m, report = load_skill_manifest(skill)
        assert report.ok
        assert any("vendor_specific_input" in w.rule for w in report.warnings)


# ─── loader edge cases ────────────────────────────────────────────────


class TestLoader:
    def test_missing_manifest_returns_error(self, tmp_path):
        m, report = load_skill_manifest(tmp_path / "nonexistent")
        assert m is None
        assert any("manifest.missing" in e.rule for e in report.errors)

    def test_malformed_yaml_rejected(self, tmp_path):
        skill_dir = tmp_path / "broken"
        skill_dir.mkdir()
        (skill_dir / SKILL_MANIFEST_FILENAME).write_text("{not valid yaml: {")
        m, report = load_skill_manifest(skill_dir)
        assert m is None
        assert any("yaml_parse" in e.rule for e in report.errors)

    def test_schema_failure_rejected(self, tmp_path):
        skill_dir = tmp_path / "bad_schema"
        skill_dir.mkdir()
        (skill_dir / SKILL_MANIFEST_FILENAME).write_text(
            "identity:\n  workflow_id: x\n# missing required fields\n"
        )
        m, report = load_skill_manifest(skill_dir)
        assert m is None
        assert any("schema" in e.rule for e in report.errors)

    def test_discover_skills_finds_sample(self):
        skills = discover_skills(SAMPLE_SKILL_DIR.parent)
        assert SAMPLE_SKILL_DIR in skills


# ─── sample skill validates ───────────────────────────────────────────


class TestSampleSkill:
    def test_sample_validates_clean(self):
        m, report = load_skill_manifest(SAMPLE_SKILL_DIR)
        assert m is not None
        assert report.ok, report.summary()
        assert m.identity.workflow_id == "sample-echo-classifier"

    def test_sample_has_three_eval_files(self):
        m, _ = load_skill_manifest(SAMPLE_SKILL_DIR)
        assert m is not None
        for fname in ("canaries.jsonl", "edge_cases.jsonl", "workflow_regression.jsonl"):
            assert (SAMPLE_SKILL_DIR / fname).exists()

    def test_sample_has_no_warnings(self):
        m, report = load_skill_manifest(SAMPLE_SKILL_DIR)
        assert m is not None
        assert not report.warnings, report.summary()

    def test_sample_demonstrates_full_protocol(self):
        m, _ = load_skill_manifest(SAMPLE_SKILL_DIR)
        assert m is not None
        # Has multi-tier cascade
        assert len(m.cascade) >= 2
        # Has eval slots populated
        assert m.eval.get("canaries").path == "canaries.jsonl"
        assert m.eval.get("edge_cases").path == "edge_cases.jsonl"
        assert m.eval.get("workflow").path == "workflow_regression.jsonl"
        # Has outputs declared
        assert len(m.outputs) >= 1
        # Has policy configured
        assert m.policy.iteration_cap > 0
