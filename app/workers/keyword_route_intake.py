"""Worker for operator-approved deterministic email keyword routes."""

from __future__ import annotations

from app.connectors.gmail_labels import GmailLabelConnector
from app.learning.email_keyword_routes import (
    KeywordRouteMatchScope,
    EmailKeywordRouteRuleStore,
    build_email_keyword_route_rule,
)
from app.shared.config import Settings
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import GmailThreadLabelRequest, gmail_level1_label_name
from app.workers.keyword_route_intake_models import KeywordRouteIntakeResult


class KeywordRouteIntakeWorker:
    """Record one operator-approved deterministic keyword route."""

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings
        self._rule_store = EmailKeywordRouteRuleStore(settings.local_email_keyword_route_log_path)

    def apply_rule(
        self,
        *,
        message_reference: str,
        level1_classification: str,
        match_scope: KeywordRouteMatchScope,
        match_value: str,
        source_thread_id: str | None,
        source_subject: str | None,
        reason: str | None,
        requested_by: str | None = None,
        label_connector: GmailLabelConnector | None = None,
    ) -> KeywordRouteIntakeResult:
        rule = build_email_keyword_route_rule(
            level1_classification=level1_classification,  # type: ignore[arg-type]
            match_scope=match_scope,
            match_value=match_value,
            source_message_reference=message_reference,
            source_thread_id=source_thread_id,
            source_subject=source_subject,
            requested_by=requested_by,
            reason=reason,
        )
        append_summary = self._rule_store.record_rule(rule=rule)
        tool_records = [
            ToolExecutionRecord(
                tool_id="keyword_route_rule_recorder",
                tool_kind="keyword_route_rule_recorder",
                status=ToolExecutionStatus.COMPLETED,
                details={
                    "rule_id": rule.rule_id,
                    "message_reference": message_reference,
                    "level1_classification": level1_classification,
                    "match_scope": match_scope,
                    "match_value": rule.match_value,
                    "recorded": append_summary["recorded"],
                    "duplicates_skipped": append_summary["duplicates_skipped"],
                },
            )
        ]
        label_applied = False
        applied_label_names: list[str] = []
        if label_connector is not None and source_thread_id:
            label_name = gmail_level1_label_name(level1_classification=level1_classification)  # type: ignore[arg-type]
            if not label_name:
                raise ValueError(
                    f"Keyword route intake cannot apply a Gmail label for {level1_classification!r}."
                )
            label_result = label_connector.apply_thread_labels(
                GmailThreadLabelRequest(
                    thread_id=source_thread_id,
                    label_names=[label_name],
                    archive_from_inbox=False,
                    clear_taxonomy_labels=False,
                )
            )
            label_applied = True
            applied_label_names = list(label_result.applied_label_names)
            tool_records.append(
                ToolExecutionRecord(
                    tool_id="keyword_route_label_applier",
                    tool_kind="keyword_route_label_applier",
                    status=ToolExecutionStatus.COMPLETED,
                    details={
                        "thread_id": source_thread_id,
                        "applied_label_names": label_result.applied_label_names,
                        "created_label_names": label_result.created_label_names,
                        "archived_from_inbox": label_result.archived_from_inbox,
                    },
                )
            )
        return KeywordRouteIntakeResult(
            message_reference=message_reference.strip(),
            level1_classification=level1_classification,  # type: ignore[arg-type]
            keyword_route_match_scope=match_scope,
            keyword_route_match_value=rule.match_value,
            source_thread_id=source_thread_id.strip() if source_thread_id else None,
            source_subject=source_subject.strip() if source_subject else None,
            recorded=append_summary["recorded"] > 0,
            duplicates_skipped=append_summary["duplicates_skipped"],
            label_applied=label_applied,
            applied_label_names=applied_label_names,
            tool_records=tool_records,
        )
