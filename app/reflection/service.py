"""Suggestion-only reflection service.

This is the first implementation of "Step 5: Reflection agent." It reads
completed workflow history and proposes improvements, but it never edits
prompts, policies, workflows, or code on its own.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from app.observability.audit import AuditLogger
from app.observability.parser import AuditLogParser
from app.observability.run_store import RunStore
from app.reflection.models import ReflectionFinding, ReflectionReport
from app.shared.models import RunStatus
from app.shared.run_ids import new_id


class ReflectionService:
    """Generate reflective reports from completed workflow activity."""

    def __init__(
        self, run_store: RunStore, parser: AuditLogParser, audit_logger: AuditLogger
    ) -> None:
        self.run_store = run_store
        self.parser = parser
        self.audit_logger = audit_logger

    def generate_report(self, workflow_id: str, limit_runs: int = 20) -> ReflectionReport:
        """Scan recent runs and emit suggestion-only findings.

        The logic is intentionally lightweight for v1: it looks for failed runs,
        stuck approvals, and low-confidence classifications because those are
        the earliest signals of workflow friction in a local-first system.
        """

        runs = [
            run
            for run in self.run_store.list_runs(limit=limit_runs)
            if run.workflow_id == workflow_id
        ]
        findings: list[ReflectionFinding] = []
        event_counter: Counter[str] = Counter()
        low_confidence_hits = 0

        # Reflection works entirely from stored state and audit events so it
        # can be replayed and reviewed without re-running the workflow itself.
        for run in runs:
            for event in self.parser.events_for_run(run.run_id):
                event_counter.update([str(event["event_type"])])
                if event["event_type"] == "worker.message.classified":
                    confidence = float(event["payload"].get("confidence", 1.0))
                    if confidence < 0.75:
                        low_confidence_hits += 1

        failed_runs = sum(1 for run in runs if run.status is RunStatus.FAILED)
        pending_approvals = event_counter["approval.requested"] - event_counter["approval.approved"]

        if failed_runs:
            findings.append(
                ReflectionFinding(
                    category="reliability",
                    severity="high",
                    message=f"{failed_runs} recent runs failed.",
                    suggestion=(
                        "Add richer connector validation or retries before worker execution."
                    ),
                )
            )
        if pending_approvals > 0:
            findings.append(
                ReflectionFinding(
                    category="approvals",
                    severity="medium",
                    message="Some actions are waiting on operator review.",
                    suggestion=(
                        "Pre-stage approval requests in the dashboard so operators "
                        "can review them in batches."
                    ),
                )
            )
        if low_confidence_hits > 0:
            findings.append(
                ReflectionFinding(
                    category="workflow_design",
                    severity="medium",
                    message="Low-confidence classifications appeared in recent runs.",
                    suggestion=(
                        "Review the email classification prompt and keyword rules, "
                        "then update them through normal code review."
                    ),
                )
            )
        if not runs:
            findings.append(
                ReflectionFinding(
                    category="observability",
                    severity="low",
                    message="No completed runs exist yet for this workflow.",
                    suggestion=(
                        "Trigger the workflow locally and inspect the resulting audit "
                        "trail before adding new integrations."
                    ),
                )
            )

        summary = (
            f"Reflection scanned {len(runs)} run(s), "
            f"found {failed_runs} failed run(s), and observed {len(findings)} suggestion(s)."
        )
        # The report is stored like any other artifact of system behavior so it
        # remains visible in the same operator tooling as the workflows.
        report = ReflectionReport(
            report_id=new_id("rpt"),
            workflow_id=workflow_id,
            generated_at=datetime.now(UTC),
            summary=summary,
            findings=findings,
            source_run_ids=[run.run_id for run in runs],
        )
        self.run_store.save_reflection_report(report)
        self.audit_logger.append_event(
            run_id="system",
            workflow_id=workflow_id,
            actor="reflection-service",
            component="reflection",
            event_type="reflection.generated",
            payload={
                "report_id": report.report_id,
                "finding_count": len(report.findings),
                "source_run_count": len(report.source_run_ids),
            },
        )
        return report

    def list_reports(self, limit: int = 10) -> list[ReflectionReport]:
        """Return recent reflection reports for the local dashboard."""

        return self.run_store.list_reflection_reports(limit=limit)
