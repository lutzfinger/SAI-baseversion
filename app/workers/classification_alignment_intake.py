"""Worker for approval-backed email classification prompt and dataset alignments."""

from __future__ import annotations

from app.learning.email_classification_alignment import (
    ClassificationAlignmentRuleStore,
    EmailClassificationDatasetOverlayStore,
    build_classification_alignment_rule,
    build_email_classification_dataset_overlay_row,
    load_classification_alignment_rules,
    write_classification_alignment_addendum,
)
from app.shared.config import Settings
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.classification_alignment_intake_models import (
    ClassificationAlignmentIntakeResult,
)
from app.workers.classification_correction import (
    ClassificationCorrectionWorker,
    resolve_classification_correction_source_example,
)
from app.workers.email_models import EmailClassification, Level1Classification, Level2Intent


class ClassificationAlignmentIntakeWorker:
    """Apply one operator-approved classification alignment across live overlays."""

    def __init__(
        self,
        *,
        settings: Settings,
        correction_worker: ClassificationCorrectionWorker,
    ) -> None:
        self.settings = settings
        self.correction_worker = correction_worker
        self.rule_store = ClassificationAlignmentRuleStore(
            settings.local_email_classification_alignment_log_path
        )
        self.dataset_overlay_store = EmailClassificationDatasetOverlayStore(
            settings.local_email_classification_dataset_overlay_path
        )

    def apply_alignment(
        self,
        *,
        run_id: str,
        workflow_id: str,
        message_reference: str,
        level1_classification: Level1Classification | None,
        level2_intent: Level2Intent | None,
        correction_reason: str | None,
        requested_by: str | None = None,
    ) -> ClassificationAlignmentIntakeResult:
        source = resolve_classification_correction_source_example(
            self.settings.local_cloud_comparison_log_path,
            message_reference=message_reference,
        )
        correction_result = self.correction_worker.apply_correction(
            run_id=run_id,
            workflow_id=workflow_id,
            message_reference=message_reference,
            level1_classification=level1_classification,
            level2_intent=level2_intent,
            correction_reason=correction_reason,
            requested_by=requested_by,
        )
        corrected = EmailClassification(
            message_id=source.message.message_id,
            level1_classification=(
                level1_classification
                or correction_result.corrected_classification.level1_classification
            ),
            level2_intent=level2_intent or correction_result.corrected_classification.level2_intent,
            confidence=1.0,
            reason=(
                correction_reason or "Operator-approved classification alignment example."
            ).strip(),
        )

        rule = build_classification_alignment_rule(
            message_reference=message_reference,
            message=source.message,
            corrected_level1_classification=corrected.level1_classification,
            corrected_level2_intent=corrected.level2_intent,
            correction_reason=correction_reason,
            requested_by=requested_by,
        )
        rule_summary = self.rule_store.record_rule(rule=rule)
        rules = load_classification_alignment_rules(
            self.settings.local_email_classification_alignment_log_path
        )
        addendum_summary = write_classification_alignment_addendum(
            path=self.settings.local_email_classification_alignment_addendum_path,
            rules=rules,
        )

        overlay_row = build_email_classification_dataset_overlay_row(
            message=source.message,
            corrected_level1_classification=corrected.level1_classification,
            corrected_level2_intent=corrected.level2_intent,
            correction_reason=correction_reason,
            requested_by=requested_by,
        )
        dataset_summary = self.dataset_overlay_store.record_example(row=overlay_row)

        tool_records = list(correction_result.tool_records)
        tool_records.append(
            ToolExecutionRecord(
                tool_id="classification_alignment_rule_recorder",
                tool_kind="classification_alignment_rule_recorder",
                status=ToolExecutionStatus.COMPLETED,
                details={
                    "rule_id": rule.rule_id,
                    "recorded": rule_summary["recorded"],
                    "duplicates_skipped": rule_summary["duplicates_skipped"],
                    "prompt_addendum_path": addendum_summary["path"],
                    "prompt_addendum_sha256": addendum_summary["sha256"],
                    "prompt_addendum_rule_count": addendum_summary["rule_count"],
                },
            )
        )
        tool_records.append(
            ToolExecutionRecord(
                tool_id="classification_alignment_dataset_overlay_recorder",
                tool_kind="classification_alignment_dataset_overlay_recorder",
                status=ToolExecutionStatus.COMPLETED,
                details={
                    "dataset_entry_id": overlay_row.dataset_entry_id,
                    "recorded": dataset_summary["recorded"],
                    "duplicates_skipped": dataset_summary["duplicates_skipped"],
                    "overlay_path": str(
                        self.settings.local_email_classification_dataset_overlay_path
                    ),
                },
            )
        )

        return ClassificationAlignmentIntakeResult(
            message_reference=message_reference.strip(),
            matched_message=source.message,
            corrected_classification=corrected,
            training_record_id=correction_result.training_record_id,
            training_recorded=correction_result.recorded,
            training_duplicates_skipped=correction_result.duplicates_skipped,
            alignment_rule_id=rule.rule_id,
            alignment_recorded=rule_summary["recorded"] > 0,
            alignment_duplicates_skipped=rule_summary["duplicates_skipped"],
            dataset_entry_id=overlay_row.dataset_entry_id,
            dataset_recorded=dataset_summary["recorded"] > 0,
            dataset_duplicates_skipped=dataset_summary["duplicates_skipped"],
            prompt_addendum_path=addendum_summary["path"],
            prompt_addendum_sha256=addendum_summary["sha256"],
            prompt_addendum_rule_count=int(addendum_summary["rule_count"]),
            correction_reason=correction_reason.strip() if correction_reason else None,
            tool_records=tool_records,
        )
