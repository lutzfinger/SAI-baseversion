"""Tests for the eval_add apply path (v2b).

The runner (overlay merge + regression) is stubbed so tests stay fast
+ offline. We verify:

  * Required-field rejection when disambiguation didn't populate
  * Idempotency: re-applying the same edge_case_id is a no-op
  * Successful append: row lands in eval/edge_cases.jsonl
  * Canary rollback: edge_cases.jsonl restored on canary failure
  * LLM-regression rollback: edge_cases.jsonl restored on material drop
  * Empty-file bootstrap: works on a brand-new edge_cases.jsonl
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.eval.proposal_apply import (
    MergeResult,
    RegressionResult,
    apply_proposal,
)


class _StubRunner:
    """Records all merge + regression calls and replays canned responses."""

    def __init__(
        self,
        *,
        merge_ok: bool = True,
        canary_failures: int = 0,
        llm_p_r_after: float = 0.85,
        llm_p_r_baseline: float = 0.85,
    ) -> None:
        self.merge_ok = merge_ok
        self.canary_failures = canary_failures
        self.llm_p_r_after = llm_p_r_after
        self.llm_p_r_baseline = llm_p_r_baseline
        self.merge_calls = 0
        self.regression_calls = 0

    def run_overlay_merge(
        self, *, public: Path, private: Path, out: Path,
    ) -> MergeResult:
        self.merge_calls += 1
        return MergeResult(ok=self.merge_ok, message="" if self.merge_ok else "boom")

    def run_full_regression(
        self, *, runtime_root: Path, phase_callback=None,
    ) -> RegressionResult:
        self.regression_calls += 1
        return RegressionResult(
            canary_passes=10 - self.canary_failures,
            canary_failures=self.canary_failures,
            canary_total=10,
            llm_p_r_baseline=self.llm_p_r_baseline,
            llm_p_r_after=self.llm_p_r_after,
            llm_p_r_drop=max(self.llm_p_r_baseline - self.llm_p_r_after, 0.0),
            summary={"stub": True},
        )


def _stage_proposal(
    tmp_path: Path,
    *,
    bucket: str = "customers",
    message_id: str = "msg_abc123",
    from_email: str = "alex@example.com",
    from_name: str | None = "Alex Apple",
    subject: str = "Re: thoughts",
    snippet: str = "hey on the fund question",
    received_at_iso: str | None = "2026-04-30T12:00:00+00:00",
    proposed_by: str = "U999",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write an eval_add proposal YAML to `tmp_path/proposal.yaml`."""

    proposal: dict[str, Any] = {
        "kind": "eval_add",
        "proposal_id": "eval_add::20260502T120000Z::test",
        "proposed_at": datetime.now(UTC).isoformat(),
        "proposed_by": proposed_by,
        "message_ref": "alex",
        "expected_level1_classification": bucket,
        "source_text": "alex should be customers",
        "resolved_message_id": message_id,
        "resolved_thread_id": message_id,
        "resolved_from_email": from_email,
        "resolved_from_name": from_name,
        "resolved_subject": subject,
        "resolved_snippet": snippet,
        "resolved_received_at_iso": received_at_iso,
    }
    if extra:
        proposal.update(extra)
    proposal_path = tmp_path / "proposal.yaml"
    proposal_path.write_text(yaml.safe_dump(proposal, sort_keys=False))
    return proposal_path


def _make_layout(tmp_path: Path, *, edge_cases_initial: str = "") -> tuple[Path, Path, Path]:
    """Create private/public/runtime roots with an edge_cases.jsonl in private."""

    private = tmp_path / "private"
    public = tmp_path / "public"
    runtime = tmp_path / "runtime"
    for p in (private, public, runtime):
        p.mkdir()
    (private / "eval").mkdir()
    (private / "eval" / "edge_cases.jsonl").write_text(edge_cases_initial)
    return private, public, runtime


# ─── tests ──────────────────────────────────────────────────────────────


class TestApplyEvalAddRequiredFields:
    def test_missing_resolved_message_id_rejects(self, tmp_path: Path) -> None:
        proposal_path = _stage_proposal(tmp_path, message_id="")
        private, public, runtime = _make_layout(tmp_path)
        runner = _StubRunner()
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
        )
        assert result.status == "rejected_invalid_proposal"
        assert "missing required resolved fields" in result.summary
        assert runner.merge_calls == 0
        assert runner.regression_calls == 0

    def test_missing_from_email_rejects(self, tmp_path: Path) -> None:
        proposal_path = _stage_proposal(tmp_path, from_email="")
        private, public, runtime = _make_layout(tmp_path)
        runner = _StubRunner()
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
        )
        assert result.status == "rejected_invalid_proposal"


class TestApplyEvalAddIdempotency:
    def test_already_present_rejected_cleanly(self, tmp_path: Path) -> None:
        edge_case_id = "edge::msg_abc123::customers"
        existing = json.dumps({"edge_case_id": edge_case_id, "stub": True}) + "\n"
        proposal_path = _stage_proposal(tmp_path)
        private, public, runtime = _make_layout(tmp_path, edge_cases_initial=existing)
        runner = _StubRunner()
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
        )
        assert result.status == "rejected_already_applied"
        # No merge or regression because we short-circuited.
        assert runner.merge_calls == 0
        assert runner.regression_calls == 0
        # File should not have been touched.
        assert (private / "eval" / "edge_cases.jsonl").read_text() == existing


class TestApplyEvalAddSuccess:
    def test_appends_and_returns_applied(self, tmp_path: Path) -> None:
        proposal_path = _stage_proposal(tmp_path)
        private, public, runtime = _make_layout(tmp_path)
        runner = _StubRunner(llm_p_r_after=0.88, llm_p_r_baseline=0.85)
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
        )
        assert result.status == "applied", result.summary
        assert "✅ Added to the eval set" in result.summary
        assert "alex@example.com" in result.summary
        assert "L1/customers" in result.summary

        # Row landed in the file.
        contents = (private / "eval" / "edge_cases.jsonl").read_text().strip()
        assert contents
        row = json.loads(contents.splitlines()[-1])
        assert row["edge_case_id"] == "edge::msg_abc123::customers"
        assert row["from_email"] == "alex@example.com"
        assert row["expected_level1_classification"] == "customers"
        assert row["source"] == "operator_loop4"
        assert row["requested_by"] == "U999"

        # Merge ran, regression ran.
        assert runner.merge_calls == 1
        assert runner.regression_calls == 1

    def test_appends_to_nonempty_file_with_trailing_newline(
        self, tmp_path: Path,
    ) -> None:
        existing = (
            json.dumps({"edge_case_id": "edge::other::partners", "stub": True})
            + "\n"
        )
        proposal_path = _stage_proposal(tmp_path)
        private, public, runtime = _make_layout(tmp_path, edge_cases_initial=existing)
        runner = _StubRunner()
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
        )
        assert result.status == "applied"
        lines = (private / "eval" / "edge_cases.jsonl").read_text().splitlines()
        assert len(lines) == 2

    def test_appends_when_existing_lacks_trailing_newline(
        self, tmp_path: Path,
    ) -> None:
        # Older fixtures may not have a trailing newline. We must add
        # one before appending so the JSONL stays valid.
        existing = json.dumps({"edge_case_id": "edge::other::partners"})
        proposal_path = _stage_proposal(tmp_path)
        private, public, runtime = _make_layout(tmp_path, edge_cases_initial=existing)
        runner = _StubRunner()
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
        )
        assert result.status == "applied"
        text = (private / "eval" / "edge_cases.jsonl").read_text()
        lines = text.splitlines()
        assert len(lines) == 2
        # Both lines should parse as JSON.
        for line in lines:
            json.loads(line)

    def test_creates_file_if_missing(self, tmp_path: Path) -> None:
        proposal_path = _stage_proposal(tmp_path)
        private, public, runtime = _make_layout(tmp_path)
        # Delete the file we just created to simulate brand-new repo.
        (private / "eval" / "edge_cases.jsonl").unlink()
        runner = _StubRunner()
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
        )
        assert result.status == "applied"
        text = (private / "eval" / "edge_cases.jsonl").read_text()
        assert text.strip()
        assert json.loads(text.strip().splitlines()[0])["edge_case_id"] == (
            "edge::msg_abc123::customers"
        )


class TestApplyEvalAddRollback:
    def test_canary_failure_restores_original_bytes(self, tmp_path: Path) -> None:
        existing = json.dumps({"edge_case_id": "edge::other::partners"}) + "\n"
        proposal_path = _stage_proposal(tmp_path)
        private, public, runtime = _make_layout(tmp_path, edge_cases_initial=existing)
        runner = _StubRunner(canary_failures=2)
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
        )
        assert result.status == "rejected_canary_failed"
        assert "Reverted" in result.summary
        # File restored byte-perfectly.
        assert (private / "eval" / "edge_cases.jsonl").read_text() == existing
        # Two merges: pre-regression + rollback.
        assert runner.merge_calls == 2

    def test_llm_regression_drop_restores_original_bytes(self, tmp_path: Path) -> None:
        existing = json.dumps({"edge_case_id": "edge::other::partners"}) + "\n"
        proposal_path = _stage_proposal(tmp_path)
        private, public, runtime = _make_layout(tmp_path, edge_cases_initial=existing)
        runner = _StubRunner(llm_p_r_baseline=0.90, llm_p_r_after=0.70)
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
        )
        assert result.status == "rejected_regression_failed"
        assert "Reverted" in result.summary
        assert (private / "eval" / "edge_cases.jsonl").read_text() == existing
        assert runner.merge_calls == 2

    def test_dry_run_returns_preview_without_committing(self, tmp_path: Path) -> None:
        """Dry-run runs the full pipeline (edit + merge + regression)
        but rolls back before commit. Returns status='dry_run_preview'
        and dry_run=True. Source file untouched."""

        existing = json.dumps({"edge_case_id": "edge::other::partners"}) + "\n"
        proposal_path = _stage_proposal(tmp_path)
        private, public, runtime = _make_layout(tmp_path, edge_cases_initial=existing)
        runner = _StubRunner()
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
            dry_run=True,
        )
        assert result.status == "dry_run_preview"
        assert result.dry_run is True
        assert "Preview only" in result.summary
        # Source file restored byte-perfectly.
        assert (private / "eval" / "edge_cases.jsonl").read_text() == existing
        # Two merges: pre-regression + rollback.
        assert runner.merge_calls == 2
        # No edited files reported (we rolled back).
        assert result.edited_files == []
        assert result.rollback_token is None

    def test_phase_callback_fires_after_canary(self, tmp_path: Path) -> None:
        """Async UX polish: callback fires twice — canary then llm — so
        the bot can post mid-results instead of waiting ~5min."""

        proposal_path = _stage_proposal(tmp_path)
        private, public, runtime = _make_layout(tmp_path)

        callback_calls: list[tuple[str, dict]] = []

        def cb(phase: str, payload: dict) -> None:
            callback_calls.append((phase, payload))

        # Use a runner that hooks into phase_callback like _DefaultRunner.
        class _CallbackStubRunner(_StubRunner):
            def run_full_regression(
                self, *, runtime_root, phase_callback=None,
            ):
                if phase_callback:
                    phase_callback("canary", {
                        "passes": 10 - self.canary_failures,
                        "failures": self.canary_failures,
                        "total": 10,
                    })
                result = super().run_full_regression(runtime_root=runtime_root)
                if phase_callback and result.canary_failures == 0:
                    phase_callback("llm", {
                        "p_r_baseline": result.llm_p_r_baseline,
                        "p_r_after": result.llm_p_r_after,
                        "p_r_drop": result.llm_p_r_drop,
                    })
                return result

        runner = _CallbackStubRunner()
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
            phase_callback=cb,
        )
        assert result.status == "applied"
        # Both phases fired in order.
        assert [c[0] for c in callback_calls] == ["canary", "llm"]
        assert callback_calls[0][1]["failures"] == 0

    def test_overlay_merge_failure_rolls_back_before_regression(
        self, tmp_path: Path,
    ) -> None:
        existing = json.dumps({"edge_case_id": "edge::other::partners"}) + "\n"
        proposal_path = _stage_proposal(tmp_path)
        private, public, runtime = _make_layout(tmp_path, edge_cases_initial=existing)
        runner = _StubRunner(merge_ok=False)
        result = apply_proposal(
            proposal_path=proposal_path,
            private_root=private, public_root=public, runtime_root=runtime,
            runner=runner,
        )
        assert result.status == "rejected_apply_error"
        assert "Overlay re-merge failed" in result.summary
        # File restored even though the failure was at merge time, not
        # regression time.
        assert (private / "eval" / "edge_cases.jsonl").read_text() == existing
        # Regression should NOT have run (gate refused before we got there).
        assert runner.regression_calls == 0
