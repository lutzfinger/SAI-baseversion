"""Worker for operator-approved newsletter lane rules."""

from __future__ import annotations

import json
from pathlib import Path

from app.learning.local_cloud_comparison import (
    LocalCloudComparisonExample,
    parse_local_cloud_comparison_payload,
)
from app.learning.newsletter_lane_registry import (
    NewsletterLaneMatchScope,
    NewsletterLaneRoute,
    NewsletterLaneRuleStore,
    build_newsletter_lane_rule,
)
from app.shared.config import Settings
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.newsletter_lane_intake_models import NewsletterLaneIntakeResult


class NewsletterLaneIntakeWorker:
    """Record one human-approved newsletter routing rule."""

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings
        self._rule_store = NewsletterLaneRuleStore(settings.newsletter_lane_rule_log_path)

    def apply_rule(
        self,
        *,
        message_reference: str,
        route: NewsletterLaneRoute,
        match_scope: NewsletterLaneMatchScope,
        reason: str | None,
        requested_by: str | None = None,
    ) -> NewsletterLaneIntakeResult:
        source = _resolve_source_example(
            self.settings.local_cloud_comparison_log_path,
            message_reference=message_reference,
        )
        match_value = _match_value(source=source, match_scope=match_scope)
        rule = build_newsletter_lane_rule(
            route=route,
            match_scope=match_scope,
            match_value=match_value,
            source_message_reference=message_reference,
            source_message=source.message,
            requested_by=requested_by,
            reason=reason,
        )
        append_summary = self._rule_store.record_rule(rule=rule)
        tool_records = [
            ToolExecutionRecord(
                tool_id="newsletter_lane_rule_recorder",
                tool_kind="newsletter_lane_rule_recorder",
                status=ToolExecutionStatus.COMPLETED,
                details={
                    "rule_id": rule.rule_id,
                    "route": route,
                    "match_scope": match_scope,
                    "match_value": match_value,
                    "recorded": append_summary["recorded"],
                    "duplicates_skipped": append_summary["duplicates_skipped"],
                    "message_reference": message_reference,
                },
            )
        ]
        return NewsletterLaneIntakeResult(
            message_reference=message_reference.strip(),
            matched_message=source.message,
            route=route,
            match_scope=match_scope,
            match_value=match_value,
            recorded=append_summary["recorded"] > 0,
            duplicates_skipped=append_summary["duplicates_skipped"],
            tool_records=tool_records,
        )


def _resolve_source_example(
    path: Path,
    *,
    message_reference: str,
) -> LocalCloudComparisonExample:
    if not path.exists():
        raise ValueError("local/cloud comparison log is empty; no messages are available to match")
    reference = message_reference.strip().lower()
    exact_matches: list[LocalCloudComparisonExample] = []
    fuzzy_matches: list[LocalCloudComparisonExample] = []
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            example = parse_local_cloud_comparison_payload(payload)
            message = example.message
            if reference in {
                message.message_id.lower(),
                (message.thread_id or "").lower(),
                example.example_id.lower(),
            }:
                exact_matches.append(example)
                continue
            if reference and reference in message.subject.lower():
                fuzzy_matches.append(example)
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        raise ValueError(f"message reference {message_reference!r} matched multiple exact examples")
    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0]
    if len(fuzzy_matches) > 1:
        raise ValueError(f"message reference {message_reference!r} is ambiguous; use message id")
    raise ValueError(f"could not find a compared message matching {message_reference!r}")


def _match_value(
    *,
    source: LocalCloudComparisonExample,
    match_scope: NewsletterLaneMatchScope,
) -> str:
    sender = source.message.from_email.strip().lower()
    if match_scope == "sender_email":
        return sender
    if "@" not in sender:
        raise ValueError("cannot create sender_domain rule for a message without a sender domain")
    return sender.rsplit("@", 1)[1]
