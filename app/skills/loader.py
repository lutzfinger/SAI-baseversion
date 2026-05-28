"""Load + validate SAI skill manifests.

Two-phase check (PRINCIPLES.md §33):

  1. **Schema validation** via the SkillManifest Pydantic model.
     Catches typos, missing required slots, wrong types.

  2. **Filesystem + cross-field contract validation**:
     - eval files exist and meet min_count
     - tool surface is non-empty when any tier is `agent`
     - propose_only tools require a two-phase commit path
     - mutate_with_approval tools require policy.approval_required
     - side-effect outputs without requires_approval need a `human` tier

Hard-contract violations become `ValidationIssue(severity=error)` and
the manifest is rejected. Soft-contract issues become warnings — the
manifest registers but the operator sees the warning before first run.

Usage:

  manifest, report = load_skill_manifest(Path("path/to/skill"))
  if not report.ok:
      print(report.summary())
      raise SystemExit(1)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

from app.skills.integrity import (
    INTEGRITY_FILENAME,
    SkillIntegrityError,
    verify_skill_integrity,
)
from app.skills.manifest import (
    SkillManifest,
    ValidationIssue,
    ValidationReport,
)

LOGGER = logging.getLogger(__name__)


SKILL_MANIFEST_FILENAME = "skill.yaml"


def load_skill_manifest(
    skill_dir: Path,
) -> tuple[Optional[SkillManifest], ValidationReport]:
    """Load + validate the manifest at ``skill_dir/skill.yaml``.

    Returns:
      (manifest, report) — manifest is None when schema validation
      fails. report.ok is True only when there are zero error-severity
      issues; warnings don't block.
    """

    manifest_path = skill_dir / SKILL_MANIFEST_FILENAME
    if not manifest_path.exists():
        return None, ValidationReport(
            workflow_id="(unknown)",
            errors=[ValidationIssue(
                severity="error",
                rule="manifest.missing",
                message=f"No {SKILL_MANIFEST_FILENAME} at {skill_dir}",
            )],
        )

    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, ValidationReport(
            workflow_id="(unparseable)",
            errors=[ValidationIssue(
                severity="error",
                rule="manifest.yaml_parse",
                message=f"YAML parse failed: {exc}",
            )],
        )

    try:
        manifest = SkillManifest.model_validate(raw)
    except Exception as exc:
        return None, ValidationReport(
            workflow_id=str((raw or {}).get("identity", {}).get("workflow_id", "(invalid)")),
            errors=[ValidationIssue(
                severity="error",
                rule="manifest.schema",
                message=f"schema validation failed: {exc}",
            )],
        )

    report = validate_skill_manifest(manifest, skill_dir)
    return manifest, report


def validate_skill_manifest(
    manifest: SkillManifest, skill_dir: Path,
) -> ValidationReport:
    """Run the cross-field + filesystem contract checks."""

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    workflow_id = manifest.identity.workflow_id

    # ── eval datasets: required kinds present + files exist + meet min_count ──
    # Per principle #16a (revised): every workflow MUST declare canaries,
    # edge_cases, and workflow datasets. true_north and disagreement_queue
    # are optional. The discriminated union in SkillEval enforces shape;
    # we enforce REQUIRED-KINDS + filesystem here.
    REQUIRED_KINDS = {"canaries", "edge_cases", "workflow"}
    kinds_present = manifest.eval.kinds_present()
    for required in REQUIRED_KINDS:
        if required not in kinds_present:
            errors.append(ValidationIssue(
                severity="error",
                rule=f"eval.required_kind_missing.{required}",
                message=(
                    f"manifest must declare an eval dataset of kind "
                    f"'{required}'. Found: {sorted(kinds_present)}"
                ),
            ))

    for spec in manifest.eval.datasets:
        eval_path = skill_dir / spec.path
        if not eval_path.exists():
            errors.append(ValidationIssue(
                severity="error",
                rule=f"eval.{spec.kind}.missing",
                message=(
                    f"manifest declares eval dataset of kind '{spec.kind}' "
                    f"at path='{spec.path}' but the file doesn't exist "
                    f"at {eval_path}"
                ),
            ))
            continue
        n = sum(1 for line in eval_path.read_text().splitlines() if line.strip())
        if n < spec.min_count:
            errors.append(ValidationIssue(
                severity="error",
                rule=f"eval.{spec.kind}.below_min_count",
                message=(
                    f"eval dataset '{spec.kind}' has {n} rows but "
                    f"min_count={spec.min_count}"
                ),
            ))

    # ── tool surface required when any tier is `agent` ────────────────
    has_agent_tier = any(t.kind == "agent" for t in manifest.cascade)
    if has_agent_tier and not manifest.tools:
        errors.append(ValidationIssue(
            severity="error",
            rule="tools.agent_tier_requires_surface",
            message=(
                "cascade has an `agent` tier but no tools[] declared. "
                "Agents need a bounded tool surface (PRINCIPLES.md §16f)."
            ),
        ))

    # ── propose_only tools need two-phase commit somewhere ─────────────
    for tool in manifest.tools:
        if tool.rights == "propose_only":
            # Two-phase commit = there's an `approval_required: true` on
            # at least one output OR policy.approval_required=true.
            two_phase_present = (
                manifest.policy.approval_required
                or any(o.requires_approval for o in manifest.outputs)
            )
            if not two_phase_present:
                errors.append(ValidationIssue(
                    severity="error",
                    rule="tools.propose_only_needs_two_phase_commit",
                    message=(
                        f"tool {tool.tool_id!r} has rights=propose_only but "
                        "no two-phase commit path exists "
                        "(policy.approval_required=false AND no "
                        "outputs[].requires_approval=true)."
                    ),
                ))

    # ── mutate_with_approval tools need policy.approval_required ──────
    for tool in manifest.tools:
        if tool.rights == "mutate_with_approval" and not manifest.policy.approval_required:
            errors.append(ValidationIssue(
                severity="error",
                rule="tools.mutate_requires_approval_policy",
                message=(
                    f"tool {tool.tool_id!r} has rights=mutate_with_approval "
                    "but policy.approval_required is false. The tool can "
                    "mutate state without a gate (#9 violated)."
                ),
            ))

    # ── side-effect outputs need a gate (#2 policy before side effects).
    # A gated output (reply/send/post/external_write/browser_submit) is OK if:
    #   - requires_approval=true (per-run two-phase commit), OR
    #   - a `human` cascade tier exists, OR
    #   - it is explicitly pre_approved=true AND a `second_opinion` tier exists.
    # `pre_approved` is the first-class "approved once at skill sign-off"
    # posture (mirrors registry approval_behavior preapproved_per_skill_signoff).
    # It MUST be paired with a different-LLM second_opinion safety gate, and it
    # must be set deliberately — a bare requires_approval=false never ungates a
    # side effect. external_write + browser_submit are gated so a pre-approved
    # web-form submit is covered (operator requirement, 2026-05-28).
    SIDE_EFFECT_OUTPUTS = {
        "reply", "send", "post", "external_write", "browser_submit",
    }
    has_human_tier = any(t.kind == "human" for t in manifest.cascade)
    has_second_opinion = any(t.kind == "second_opinion" for t in manifest.cascade)
    for out in manifest.outputs:
        if out.side_effect not in SIDE_EFFECT_OUTPUTS:
            continue
        if out.requires_approval or has_human_tier:
            continue  # gated by per-run approval or a human tier
        if out.pre_approved:
            if not has_second_opinion:
                errors.append(ValidationIssue(
                    severity="error",
                    rule="outputs.pre_approved_needs_second_opinion",
                    message=(
                        f"output {out.name!r} is pre_approved=true but the cascade "
                        "has no `second_opinion` tier. A pre-approved side effect "
                        "MUST carry a different-LLM 'is this safe and wanted?' gate."
                    ),
                ))
            continue
        errors.append(ValidationIssue(
            severity="error",
            rule="outputs.side_effect_needs_gate",
            message=(
                f"output {out.name!r} has side_effect={out.side_effect} but no "
                "gate: set requires_approval=true, add a `human` tier, or mark it "
                "pre_approved=true WITH a `second_opinion` safety gate "
                "(#2 policy before side effects)."
            ),
        ))

    # ── soft contract: cost cap warnings ──────────────────────────────
    if manifest.policy.cost_cap_per_invocation_usd > 1.0:
        warnings.append(ValidationIssue(
            severity="warning",
            rule="policy.cost_cap_high",
            message=(
                f"policy.cost_cap_per_invocation_usd="
                f"${manifest.policy.cost_cap_per_invocation_usd:.2f} per call "
                "is above $1.00 — confirm with operator before first run."
            ),
        ))
    if manifest.policy.daily_invocation_cap > 1000:
        warnings.append(ValidationIssue(
            severity="warning",
            rule="policy.daily_cap_high",
            message=(
                f"policy.daily_invocation_cap={manifest.policy.daily_invocation_cap} "
                "is above 1000 — confirm with operator."
            ),
        ))

    # ── soft contract: vendor-specific names in tool inputs ───────────
    vendor_smell = ("openai_client", "anthropic_client", "claude_client", "gpt_")
    for tool in manifest.tools:
        for input_name in tool.inputs.keys():
            if any(s in input_name.lower() for s in vendor_smell):
                warnings.append(ValidationIssue(
                    severity="warning",
                    rule="tools.vendor_specific_input",
                    message=(
                        f"tool {tool.tool_id!r} has input {input_name!r} "
                        "that looks vendor-specific. Use the Provider "
                        "abstraction (#13) so the workflow stays portable."
                    ),
                ))

    # ── integrity hash: Phase 2 (lenient) — log drift, don't block ────
    # Per docs/design_live_public_versioning.md sections D + E. Skills
    # that haven't been promoted yet (no .skill-content-sha256 file) are
    # silently allowed in Phase 2 — the operator stamps them once and
    # subsequent edits surface as warnings here. Phase 3 (post-1.0)
    # flips this to strict and fails closed.
    integrity_path = skill_dir / INTEGRITY_FILENAME
    if integrity_path.exists():
        try:
            verify_skill_integrity(skill_dir, strict=True)
        except SkillIntegrityError as exc:
            warnings.append(ValidationIssue(
                severity="warning",
                rule="integrity.drift",
                message=str(exc),
            ))
            LOGGER.warning("skill integrity drift: %s", exc)

    return ValidationReport(
        workflow_id=workflow_id,
        errors=errors,
        warnings=warnings,
    )


def discover_skills(root: Path) -> list[Path]:
    """Find all skill directories under ``root`` (containing skill.yaml)."""

    if not root.exists():
        return []
    return sorted(p.parent for p in root.glob("**/skill.yaml"))
