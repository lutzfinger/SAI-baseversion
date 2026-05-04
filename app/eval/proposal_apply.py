"""Apply staged proposals from #sai-eval (PRINCIPLES.md §16b Loop 4).

The slack_bot stages proposals to ``eval/proposed/<proposal_id>.yaml``.
This module reads them and applies them to live state with the proper
gates: full two-tier regression (canaries + LLM edge cases) BEFORE the
change ships, atomic-edit-with-rollback on the rules YAML, hash refresh
+ runtime re-merge, structured result for Slack reply.

Hard contract: this module never silently modifies anything. Every
exit path returns an ``ApplyResult`` with the action taken (or rejected)
plus the regression scores so the audit trail is complete.

v2a: add_rule path only. v2b adds eval_add (needs Gmail resolver).
"""

from __future__ import annotations

import shutil
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ApplyResult(BaseModel):
    """Structured outcome of applying one proposal."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    proposal_kind: str
    applied_at: datetime
    status: Literal[
        "applied",
        "rejected_regression_failed",
        "rejected_canary_failed",
        "rejected_already_applied",
        "rejected_invalid_proposal",
        "rejected_missing_proposal",
        "rejected_apply_error",
        "dry_run_preview",
    ]
    summary: str
    """Human-readable one-liner suitable for Slack."""

    regression_summary: Optional[dict[str, Any]] = None
    """Pre/post regression numbers when applicable."""

    edited_files: list[str] = Field(default_factory=list)
    rollback_token: Optional[str] = None
    """Path of the .pre-apply backup if a rollback would be needed."""

    dry_run: bool = False
    """True when this result came from a dry_run=True call. The change
    was simulated end-to-end (edit + merge + regression) but ROLLED
    BACK before commit. status='dry_run_preview' on success path;
    other rejected_* statuses still apply when the simulated change
    would have failed regression."""


def apply_proposal(
    *,
    proposal_path: Path,
    private_root: Path,
    public_root: Path,
    runtime_root: Path,
    runner: Optional[Any] = None,
    phase_callback: Optional[Any] = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Load a staged proposal and apply it.

    Args:
      proposal_path: ``eval/proposed/<id>.yaml``
      private_root: e.g. ``$SAI_PRIVATE`` (operator's overlay)
      public_root: e.g. ``$SAI_PUBLIC`` (cloned framework repo)
      runtime_root: e.g. ``~/.sai-runtime``
      runner: optional injection point for tests; defaults to subprocess
              calls into ``sai-overlay`` and ``sai eval run``.

    Returns:
      ApplyResult — never raises on user-facing errors; status field
      tells the caller what happened.
    """

    runner = runner or _DefaultRunner()
    if not proposal_path.exists():
        return ApplyResult(
            proposal_id="(unknown)", proposal_kind="(unknown)",
            applied_at=datetime.now(UTC),
            status="rejected_missing_proposal",
            summary=f"Proposal file not found: {proposal_path}",
        )

    try:
        proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ApplyResult(
            proposal_id="(unparseable)", proposal_kind="(unparseable)",
            applied_at=datetime.now(UTC),
            status="rejected_invalid_proposal",
            summary=f"Could not parse {proposal_path.name}: {exc}",
        )

    kind = proposal.get("kind", "")
    proposal_id = proposal.get("proposal_id", proposal_path.stem)
    if kind == "rule_add":
        return _apply_rule_add(
            proposal=proposal, proposal_id=proposal_id,
            private_root=private_root, public_root=public_root,
            runtime_root=runtime_root, runner=runner,
            phase_callback=phase_callback, dry_run=dry_run,
        )
    if kind == "eval_add":
        return _apply_eval_add(
            proposal=proposal, proposal_id=proposal_id,
            private_root=private_root, public_root=public_root,
            runtime_root=runtime_root, runner=runner,
            phase_callback=phase_callback, dry_run=dry_run,
        )
    return ApplyResult(
        proposal_id=proposal_id, proposal_kind=kind,
        applied_at=datetime.now(UTC),
        status="rejected_invalid_proposal",
        summary=f"❌ Couldn't apply: unknown proposal kind `{kind}`.",
    )


# ─── rule_add apply path ──────────────────────────────────────────────


def _apply_rule_add(
    *,
    proposal: dict[str, Any],
    proposal_id: str,
    private_root: Path,
    public_root: Path,
    runtime_root: Path,
    runner: Any,
    phase_callback: Optional[Any] = None,
    dry_run: bool = False,
) -> ApplyResult:
    target = proposal.get("target", "")
    target_kind = proposal.get("target_kind", "")
    bucket = proposal.get("expected_level1_classification", "")

    if not (target and target_kind and bucket):
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="rule_add",
            applied_at=datetime.now(UTC),
            status="rejected_invalid_proposal",
            summary=f"rule_add proposal missing required fields ({target=}, {target_kind=}, {bucket=})",
        )

    rules_yaml = private_root / "prompts" / "email" / "keyword-classify.md"
    if not rules_yaml.exists():
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="rule_add",
            applied_at=datetime.now(UTC),
            status="rejected_apply_error",
            summary=f"Rules YAML not found at {rules_yaml}",
        )

    # 1. Backup
    backup_path = rules_yaml.with_suffix(
        f".md.pre-apply-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    shutil.copy2(rules_yaml, backup_path)

    # 2. Edit (in-place, idempotent: skip if already present)
    try:
        already_present = _append_rule_to_yaml(
            yaml_path=rules_yaml,
            target=target, target_kind=target_kind, bucket=bucket,
            proposal_id=proposal_id,
        )
    except Exception as exc:
        backup_path.unlink(missing_ok=True)
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="rule_add",
            applied_at=datetime.now(UTC),
            status="rejected_apply_error",
            summary=f"YAML edit failed: {exc}",
        )

    if already_present:
        backup_path.unlink(missing_ok=True)
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="rule_add",
            applied_at=datetime.now(UTC),
            status="rejected_already_applied",
            summary=(
                f"Rule {target} → L1/{bucket} already present in "
                f"{rules_yaml.name}. No change."
            ),
        )

    # 3. Update prompt-locks.yaml hash so the runtime loader doesn't
    # block on principle #23. Also bump the sha256 alongside the edit
    # so a future merge is consistent.
    try:
        _refresh_prompt_lock(
            locks_path=private_root / "prompts" / "prompt-locks.yaml",
            prompt_relpath="email/keyword-classify.md",
            prompt_path=rules_yaml,
        )
    except Exception as exc:
        # Roll back the YAML edit.
        shutil.copy2(backup_path, rules_yaml)
        backup_path.unlink(missing_ok=True)
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="rule_add",
            applied_at=datetime.now(UTC),
            status="rejected_apply_error",
            summary=f"Prompt lock refresh failed: {exc}",
        )

    # 3.5. Regenerate canaries from the updated rules so the rule_add
    # gets its own canary in eval/canaries.jsonl. Per operator
    # 2026-05-02 evening: "any conversation that leads to a change gets
    # into the eval dataset to ensure that we test against it."
    canaries_path = private_root / "eval" / "canaries.jsonl"
    canaries_backup = (
        canaries_path.read_bytes() if canaries_path.exists() else b""
    )
    canary_result = runner.run_canary_regen(
        private_root=private_root, public_root=public_root,
    )
    if not canary_result.ok:
        shutil.copy2(backup_path, rules_yaml)
        backup_path.unlink(missing_ok=True)
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="rule_add",
            applied_at=datetime.now(UTC),
            status="rejected_apply_error",
            summary=f"Canary regeneration failed: {canary_result.message}",
        )

    # 4. Re-merge runtime so the new rule + new hash + new canaries
    # land in ~/.sai-runtime/.
    merge_result = runner.run_overlay_merge(
        public=public_root, private=private_root, out=runtime_root
    )
    if not merge_result.ok:
        shutil.copy2(backup_path, rules_yaml)
        if canaries_backup:
            canaries_path.write_bytes(canaries_backup)
        backup_path.unlink(missing_ok=True)
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="rule_add",
            applied_at=datetime.now(UTC),
            status="rejected_apply_error",
            summary=f"Overlay re-merge failed: {merge_result.message}",
        )

    # 5. Run two-tier regression as the gate. Phase callback fires
    # twice during this call — once after the canary phase (~5s) and
    # once after the LLM phase (~5min) — so the bot can post mid-results.
    regression = runner.run_full_regression(
        runtime_root=runtime_root, phase_callback=phase_callback,
    )
    summary_blob = regression.summary

    # 5a. Canary failure → hard rollback (production never reaches LLM
    # for those inputs; canary fail = real rules-tier regression).
    if regression.canary_failures > 0:
        shutil.copy2(backup_path, rules_yaml)
        if canaries_backup:
            canaries_path.write_bytes(canaries_backup)
        # Re-merge to undo the runtime change too.
        runner.run_overlay_merge(
            public=public_root, private=private_root, out=runtime_root
        )
        backup_path.unlink(missing_ok=True)
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="rule_add",
            applied_at=datetime.now(UTC),
            status="rejected_canary_failed",
            summary=(
                f"❌ Couldn't apply — the new rule broke "
                f"{regression.canary_failures} existing rule"
                f"{'s' if regression.canary_failures != 1 else ''}. "
                f"Reverted to the previous setup."
            ),
            regression_summary=summary_blob,
        )

    # 5b. LLM regression material drop → soft block (configurable).
    # For v2a we treat any drop in completed P/R as a soft block too.
    # Operator can override later via a separate mechanism.
    if regression.llm_p_r_drop > 0.10:
        shutil.copy2(backup_path, rules_yaml)
        if canaries_backup:
            canaries_path.write_bytes(canaries_backup)
        runner.run_overlay_merge(
            public=public_root, private=private_root, out=runtime_root
        )
        backup_path.unlink(missing_ok=True)
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="rule_add",
            applied_at=datetime.now(UTC),
            status="rejected_regression_failed",
            summary=(
                f"❌ Couldn't apply — accuracy on past emails dropped "
                f"too much (was {regression.llm_p_r_baseline:.0%}, "
                f"would be {regression.llm_p_r_after:.0%}). "
                f"Reverted. Maybe the rule is too broad?"
            ),
            regression_summary=summary_blob,
        )

    # 6a. Dry-run: simulated end-to-end (edit + canary regen + merge +
    # regression all ran), now ROLL BACK before commit. Operator gets
    # the regression numbers without a state change.
    if dry_run:
        shutil.copy2(backup_path, rules_yaml)
        if canaries_backup:
            canaries_path.write_bytes(canaries_backup)
        runner.run_overlay_merge(
            public=public_root, private=private_root, out=runtime_root,
        )
        backup_path.unlink(missing_ok=True)
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="rule_add",
            applied_at=datetime.now(UTC),
            status="dry_run_preview",
            summary=(
                f"_Preview only._ This rule WOULD pass: "
                f"{regression.canary_total} canaries OK, past-email accuracy "
                f"{regression.llm_p_r_after:.0%}. Nothing changed yet — "
                f"re-issue without --dry-run to apply."
            ),
            regression_summary=summary_blob,
            edited_files=[],
            rollback_token=None,
            dry_run=True,
        )

    # 6b. Success — keep backup for one rotation cycle, return result.
    return ApplyResult(
        proposal_id=proposal_id, proposal_kind="rule_add",
        applied_at=datetime.now(UTC),
        status="applied",
        summary=(
            f"✅ Done. From now on, mail from `{target}` will be tagged "
            f"`L1/{bucket}`. All {regression.canary_total} existing rules still "
            f"work; past-email accuracy held at {regression.llm_p_r_after:.0%}."
        ),
        regression_summary=summary_blob,
        edited_files=[str(rules_yaml.relative_to(private_root))],
        rollback_token=str(backup_path),
    )


# ─── eval_add apply path ──────────────────────────────────────────────


def _apply_eval_add(
    *,
    proposal: dict[str, Any],
    proposal_id: str,
    private_root: Path,
    public_root: Path,
    runtime_root: Path,
    runner: Any,
    phase_callback: Optional[Any] = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Append one resolved EdgeCaseRow to ``eval/edge_cases.jsonl`` and
    gate on regression. Source-of-truth is the private repo's edge_cases
    file; runtime gets the new row via the post-edit re-merge.

    Required proposal fields (populated by the v2b disambiguation flow
    before staging):

      - ``resolved_message_id``      — concrete Gmail message_id
      - ``resolved_from_email``      — captured at resolve time
      - ``expected_level1_classification`` — bucket the operator picked
      - ``proposed_by``              — Slack user_id (for audit)
      - ``source_text``              — original operator message
    """

    from app.eval.datasets import EdgeCaseRow
    from app.workers.email_models import LEVEL1_DISPLAY_NAMES

    bucket = proposal.get("expected_level1_classification", "")
    message_id = proposal.get("resolved_message_id", "")
    from_email = proposal.get("resolved_from_email", "")
    proposed_by = proposal.get("proposed_by", "(unknown)")
    source_text = proposal.get("source_text", "") or ""
    if not (bucket and message_id and from_email):
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="eval_add",
            applied_at=datetime.now(UTC),
            status="rejected_invalid_proposal",
            summary=(
                "eval_add proposal missing required resolved fields "
                f"({bucket=}, {message_id=}, {from_email=}). "
                "The disambiguation step should have populated them."
            ),
        )

    edge_cases_path = private_root / "eval" / "edge_cases.jsonl"
    if not edge_cases_path.exists():
        # Brand-new repos may not have the file yet — create it empty.
        edge_cases_path.parent.mkdir(parents=True, exist_ok=True)
        edge_cases_path.write_text("", encoding="utf-8")

    edge_case_id = f"edge::{message_id}::{bucket}"

    # Idempotency: skip if this exact edge_case_id is already present.
    if _edge_case_id_present(edge_cases_path, edge_case_id):
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="eval_add",
            applied_at=datetime.now(UTC),
            status="rejected_already_applied",
            summary=(
                f"That email is already in the eval set as `L1/{bucket}`. "
                f"No change."
            ),
        )

    # Build the row.
    received_at_iso = proposal.get("resolved_received_at_iso") or None
    received_at = None
    if received_at_iso:
        try:
            iso = received_at_iso.replace("Z", "+00:00")
            received_at = datetime.fromisoformat(iso)
        except Exception:
            received_at = None

    snippet = proposal.get("resolved_snippet") or ""
    try:
        row = EdgeCaseRow(
            edge_case_id=edge_case_id,
            captured_at=datetime.now(UTC),
            source="operator_loop4",
            requested_by=str(proposed_by),
            correction_reason=f"Operator Loop 4 add_eval: {source_text[:200]}",
            message_id=str(message_id),
            thread_id=proposal.get("resolved_thread_id") or str(message_id),
            from_email=str(from_email),
            from_name=proposal.get("resolved_from_name") or None,
            to=[],
            cc=[],
            subject=proposal.get("resolved_subject") or "(no subject)",
            snippet=snippet,
            body_excerpt=snippet,
            body=None,
            received_at=received_at,
            expected_level1_classification=bucket,  # type: ignore[arg-type]
            expected_level2_intent="others",
            raw_level1_label=LEVEL1_DISPLAY_NAMES.get(bucket, bucket),
            raw_level2_label="Others",
        )
    except Exception as exc:
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="eval_add",
            applied_at=datetime.now(UTC),
            status="rejected_invalid_proposal",
            summary=f"Couldn't build EdgeCaseRow: {exc}",
        )

    # Snapshot original bytes so we can restore byte-perfectly on
    # rollback. Smaller + safer than copying to a sibling file.
    original_bytes = edge_cases_path.read_bytes()
    new_line = row.model_dump_json() + "\n"
    # Make sure we end with a newline before appending so the file
    # stays valid jsonl when the previous write was missing the trailing
    # newline.
    needs_newline = (
        len(original_bytes) > 0 and not original_bytes.endswith(b"\n")
    )
    with edge_cases_path.open("ab") as fh:
        if needs_newline:
            fh.write(b"\n")
        fh.write(new_line.encode("utf-8"))

    # Re-merge runtime so the regression sees the new row.
    merge_result = runner.run_overlay_merge(
        public=public_root, private=private_root, out=runtime_root,
    )
    if not merge_result.ok:
        edge_cases_path.write_bytes(original_bytes)
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="eval_add",
            applied_at=datetime.now(UTC),
            status="rejected_apply_error",
            summary=f"Overlay re-merge failed: {merge_result.message}",
        )

    # Run the gate. Adding a single edge case shouldn't break canaries
    # (the rules tier doesn't read edge_cases), so canary failure here
    # is genuinely surprising — but we still gate on it for symmetry
    # with rule_add. The real signal is the LLM regression score.
    # Phase callback fires after canary (~5s) + after LLM (~5min).
    regression = runner.run_full_regression(
        runtime_root=runtime_root, phase_callback=phase_callback,
    )

    if regression.canary_failures > 0:
        edge_cases_path.write_bytes(original_bytes)
        runner.run_overlay_merge(
            public=public_root, private=private_root, out=runtime_root,
        )
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="eval_add",
            applied_at=datetime.now(UTC),
            status="rejected_canary_failed",
            summary=(
                f"❌ Adding that example unexpectedly broke "
                f"{regression.canary_failures} rule canary check"
                f"{'s' if regression.canary_failures != 1 else ''}. "
                f"Reverted — please check the regression report."
            ),
            regression_summary=regression.summary,
        )

    if regression.llm_p_r_drop > 0.10:
        edge_cases_path.write_bytes(original_bytes)
        runner.run_overlay_merge(
            public=public_root, private=private_root, out=runtime_root,
        )
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="eval_add",
            applied_at=datetime.now(UTC),
            status="rejected_regression_failed",
            summary=(
                f"❌ Adding that example dropped past-email accuracy "
                f"too much (was {regression.llm_p_r_baseline:.0%}, "
                f"would be {regression.llm_p_r_after:.0%}). "
                f"Reverted. The example may contradict an existing rule."
            ),
            regression_summary=regression.summary,
        )

    if dry_run:
        edge_cases_path.write_bytes(original_bytes)
        runner.run_overlay_merge(
            public=public_root, private=private_root, out=runtime_root,
        )
        return ApplyResult(
            proposal_id=proposal_id, proposal_kind="eval_add",
            applied_at=datetime.now(UTC),
            status="dry_run_preview",
            summary=(
                f"_Preview only._ Adding this email to the eval set "
                f"WOULD pass: past-email accuracy "
                f"{regression.llm_p_r_after:.0%}. Nothing changed."
            ),
            regression_summary=regression.summary,
            edited_files=[],
            rollback_token=None,
            dry_run=True,
        )

    return ApplyResult(
        proposal_id=proposal_id, proposal_kind="eval_add",
        applied_at=datetime.now(UTC),
        status="applied",
        summary=(
            f"✅ Added to the eval set: that email from "
            f"`{from_email}` will now teach the LLM to tag similar mail "
            f"`L1/{bucket}`. Past-email accuracy held at "
            f"{regression.llm_p_r_after:.0%}."
        ),
        regression_summary=regression.summary,
        edited_files=[str(edge_cases_path.relative_to(private_root))],
        rollback_token=edge_case_id,
    )


def _edge_case_id_present(edge_cases_path: Path, edge_case_id: str) -> bool:
    """Cheap scan — edge_cases.jsonl is capped at ~50 rows so a full
    read is fine. We look for the literal id string rather than parsing
    each line so a malformed historical row doesn't abort the check.
    """

    if not edge_cases_path.exists():
        return False
    needle = f'"edge_case_id":"{edge_case_id}"'
    needle_spaced = f'"edge_case_id": "{edge_case_id}"'
    raw = edge_cases_path.read_text(encoding="utf-8", errors="replace")
    return needle in raw or needle_spaced in raw


# ─── helpers ──────────────────────────────────────────────────────────


def _append_rule_to_yaml(
    *,
    yaml_path: Path,
    target: str,
    target_kind: str,
    bucket: str,
    proposal_id: str,
) -> bool:
    """Edit the keyword-classify.md frontmatter to add the new rule.

    Returns True if the rule was already present (no edit made),
    False if the rule was appended.

    Edits the YAML frontmatter in-place via text manipulation so the
    operator's other comments/structure stay intact.
    """

    raw = yaml_path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError("Expected YAML frontmatter starting with '---'")

    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Malformed frontmatter; expected '---' open and close")
    fm_text = parts[1]
    body = parts[2]

    fm = yaml.safe_load(fm_text) or {}
    classifier = fm.setdefault("classifier", {})
    if target_kind == "sender_email":
        bucket_dict = classifier.setdefault("level1_sender_email_matches", {})
    elif target_kind == "sender_domain":
        bucket_dict = classifier.setdefault("level1_sender_domain_matches", {})
    else:
        raise ValueError(f"Unsupported target_kind: {target_kind}")

    current = list(bucket_dict.get(bucket, []))
    target_norm = target.strip().lower()
    if any(str(v).strip().lower() == target_norm for v in current):
        return True
    current.append(target_norm)
    bucket_dict[bucket] = current

    new_fm_text = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
    new_raw = f"---\n{new_fm_text}---{body}"
    yaml_path.write_text(new_raw, encoding="utf-8")
    return False


def _refresh_prompt_lock(
    *, locks_path: Path, prompt_relpath: str, prompt_path: Path,
) -> None:
    import hashlib
    locks = yaml.safe_load(locks_path.read_text()) or {"prompts": {}}
    new_hash = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    locks.setdefault("prompts", {})[prompt_relpath] = new_hash
    # Preserve a stable order
    out_lines = [f'version: "{locks.get("version", "1")}"', "prompts:"]
    for key in sorted(locks["prompts"].keys()):
        out_lines.append(f"  {key}: {locks['prompts'][key]}")
    locks_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


# ─── runner abstraction (so tests can inject) ─────────────────────────


class MergeResult(BaseModel):
    ok: bool
    message: str = ""


class CanaryRegenResult(BaseModel):
    ok: bool
    canaries_written: int = 0
    message: str = ""


class RegressionResult(BaseModel):
    canary_passes: int
    canary_failures: int
    canary_total: int
    llm_p_r_baseline: float
    llm_p_r_after: float
    llm_p_r_drop: float
    summary: dict[str, Any]


class _DefaultRunner:
    """Subprocess-backed runner. Tests inject a stub instead."""

    def run_canary_regen(
        self, *, private_root: Path, public_root: Path | None = None,
    ) -> CanaryRegenResult:
        """Regenerate ``eval/canaries.jsonl`` from the current rules.

        The script lives in PUBLIC's `scripts/` (it's framework code).
        It reads PRIVATE's keyword-classify prompt + writes PRIVATE's
        eval/canaries.jsonl, driven by env-var overrides on the
        Settings paths. The next merge syncs runtime.

        Per operator decision 2026-05-02 — every change through Loop 4
        must produce a corresponding eval row.

        Bug fix 2026-05-05: previously ran with cwd=private which
        meant `python -m scripts.generate_classifier_canaries` couldn't
        find the module (script lives in public). Now runs with
        cwd=public + env vars pointing at private's dirs.
        """

        # The script imports from `app.*` and is at
        # `<public>/scripts/generate_classifier_canaries.py`. cwd=public
        # ensures `python -m scripts.X` finds it.
        if public_root is None:
            # Fallback: try to locate public as the script's parent.
            script_path = Path(__file__).resolve().parents[2]
            public_root = script_path
        cmd = [
            str(private_root / ".venv" / "bin" / "python"),
            "-m", "scripts.generate_classifier_canaries",
        ]
        if not Path(cmd[0]).exists():
            from pathlib import Path as _P
            runtime_python = _P.home() / ".sai-runtime" / ".venv" / "bin" / "python"
            if runtime_python.exists():
                cmd[0] = str(runtime_python)
        # Settings env overrides so the script reads + writes private.
        env = os.environ.copy()
        env["SAI_PROMPTS_DIR"] = str(private_root / "prompts")
        env["SAI_LEARNING_DIR"] = str(private_root / "eval")
        env["SAI_ROOT_DIR"] = str(private_root)
        env["PYTHONPATH"] = (
            f"{public_root}:{env.get('PYTHONPATH', '')}".rstrip(":")
        )
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=str(public_root), env=env,
        )
        if proc.returncode != 0:
            return CanaryRegenResult(
                ok=False,
                message=(proc.stderr or proc.stdout)[-400:],
            )
        # Best-effort parse of "wrote N canaries" line.
        written = 0
        for line in proc.stdout.splitlines():
            if "wrote" in line and "canaries" in line:
                tokens = line.split()
                for tok in tokens:
                    if tok.isdigit():
                        written = int(tok)
                        break
        return CanaryRegenResult(ok=True, canaries_written=written)

    def run_overlay_merge(
        self, *, public: Path, private: Path, out: Path,
    ) -> MergeResult:
        # As of overlay.py 2026-05-02 (PRESERVE_ON_CLEAN), `--clean`
        # preserves the runtime .venv and the cascade-written eval state
        # files. So a single merge call is sufficient — no venv recreate
        # needed. The merge takes ~3-5s instead of the prior ~30s.
        cmd_merge = [
            str(public / ".venv" / "bin" / "sai-overlay"),
            "merge", "--public", str(public),
            "--private", str(private), "--out", str(out),
            "--mode", "copy", "--clean",
        ]
        proc = subprocess.run(cmd_merge, capture_output=True, text=True)
        return MergeResult(
            ok=proc.returncode == 0,
            message=proc.stderr[-400:] if proc.returncode != 0 else "",
        )

    def run_full_regression(
        self, *, runtime_root: Path,
        phase_callback: Optional[Any] = None,
    ) -> RegressionResult:
        """Run two-tier regression as the apply gate. Per operator
        2026-05-01: LOCAL LLM ONLY — cloud disabled to keep apply cost
        at zero. Canary tier (deterministic) + LLM tier with cloud
        disabled = free regression that catches both rule-level and
        local-LLM-level impacts of the rule edit.

        ``phase_callback(phase: str, payload: dict)`` fires twice when
        provided:

          * ``phase="canary"`` after canary phase completes (~5s)
          * ``phase="llm"`` after LLM phase completes (~5min)

        The callback is best-effort — exceptions in the callback are
        swallowed so a Slack-post failure doesn't fail the apply.
        """

        report_path = runtime_root / "eval" / "runs" / (
            f"apply-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)

        # Canary phase first.
        cmd = [
            str(runtime_root / ".venv" / "bin" / "python"),
            "-m", "scripts.sai_eval", "run", "--canaries-only",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=runtime_root)
        summary: dict[str, Any] = {
            "canary_raw": proc.stdout[-1000:],
            "canary_stderr": proc.stderr[-400:],
        }
        passes = failures = total = 0
        for line in proc.stdout.splitlines():
            if line.startswith("results:"):
                tokens = line.split()
                passes = int(tokens[1])
                failures = int(tokens[4])
                total = int(tokens[7])
                break

        # Mid-result callback after canary phase. Best-effort.
        if phase_callback is not None:
            try:
                phase_callback("canary", {
                    "passes": passes, "failures": failures, "total": total,
                })
            except Exception:
                pass

        # If canary failed, no point running LLM eval — the apply is
        # already going to roll back.
        if failures > 0:
            return RegressionResult(
                canary_passes=passes, canary_failures=failures, canary_total=total,
                llm_p_r_baseline=1.0, llm_p_r_after=1.0, llm_p_r_drop=0.0,
                summary=summary,
            )

        # LLM phase — local-only (the regression script already has
        # --legacy-fixture / not flag; see TODO note for adding
        # --local-only). For v2a we invoke regression_test_email_classifier
        # with disable_cloud_tier (TODO once that arg exists). For now,
        # set the env var the regression respects.
        llm_cmd = [
            str(runtime_root / ".venv" / "bin" / "python"),
            "-m", "scripts.regression_test_email_classifier",
        ]
        env = {**__import__("os").environ, "SAI_REGRESSION_DISABLE_CLOUD": "1"}
        proc_llm = subprocess.run(
            llm_cmd, capture_output=True, text=True, cwd=runtime_root, env=env,
        )
        summary["llm_raw"] = proc_llm.stdout[-2000:]
        summary["llm_stderr"] = proc_llm.stderr[-400:]
        # Parse "Overall L1 accuracy: X%" from the report — best effort.
        baseline = 1.0
        after = 1.0
        for line in proc_llm.stdout.splitlines():
            stripped = line.strip().lower()
            if "overall l1 accuracy" in stripped or "overall accuracy" in stripped:
                # accept "Overall L1 accuracy: 0.842" or "84.2%"
                import re as _re
                m = _re.search(r"([0-9]+\.[0-9]+)%?", stripped)
                if m:
                    after = float(m.group(1))
                    if after > 1.0:
                        after = after / 100.0
                break

        result = RegressionResult(
            canary_passes=passes, canary_failures=failures, canary_total=total,
            llm_p_r_baseline=baseline, llm_p_r_after=after,
            llm_p_r_drop=max(baseline - after, 0.0),
            summary=summary,
        )
        if phase_callback is not None:
            try:
                phase_callback("llm", {
                    "p_r_baseline": baseline,
                    "p_r_after": after,
                    "p_r_drop": result.llm_p_r_drop,
                })
            except Exception:
                pass
        return result
