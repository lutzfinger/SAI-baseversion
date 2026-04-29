"""Append-only storage for operator-approved deterministic email keyword routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.workers.email_models import Level1Classification

KeywordRouteMatchScope = Literal[
    "sender_email",
    "sender_domain",
    "sender_domain_direct_address",
]


class EmailKeywordRouteRule(BaseModel):
    """One operator-approved deterministic sender/domain route."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    level1_classification: Level1Classification
    match_scope: KeywordRouteMatchScope
    match_value: str
    source_message_reference: str
    source_thread_id: str | None = None
    source_subject: str | None = None
    requested_by: str | None = None
    reason: str | None = None
    recorded_at: datetime


class EmailKeywordRouteRuleStore:
    """Append-only JSONL store for deterministic email keyword routes."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load_rules(self) -> list[EmailKeywordRouteRule]:
        if not self.path.exists():
            return []
        rules: list[EmailKeywordRouteRule] = []
        with self.path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                rules.append(EmailKeywordRouteRule.model_validate_json(line))
        return rules

    def record_rule(self, *, rule: EmailKeywordRouteRule) -> dict[str, int]:
        existing = self.load_rules()
        duplicate = next(
            (
                candidate
                for candidate in existing
                if candidate.level1_classification == rule.level1_classification
                and candidate.match_scope == rule.match_scope
                and candidate.match_value == rule.match_value
            ),
            None,
        )
        if duplicate is not None:
            return {"received": 1, "recorded": 0, "duplicates_skipped": 1}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(rule.model_dump(mode="json"), sort_keys=True))
            handle.write("\n")
        return {"received": 1, "recorded": 1, "duplicates_skipped": 0}


def build_email_keyword_route_rule(
    *,
    level1_classification: Level1Classification,
    match_scope: KeywordRouteMatchScope,
    match_value: str,
    source_message_reference: str,
    source_thread_id: str | None,
    source_subject: str | None,
    requested_by: str | None,
    reason: str | None,
) -> EmailKeywordRouteRule:
    normalized_value = match_value.strip().lower()
    rule_id = (
        f"email_keyword_route:{level1_classification}:{match_scope}:"
        f"{normalized_value}:{source_message_reference.strip()}"
    )
    return EmailKeywordRouteRule(
        rule_id=rule_id,
        level1_classification=level1_classification,
        match_scope=match_scope,
        match_value=normalized_value,
        source_message_reference=source_message_reference.strip(),
        source_thread_id=source_thread_id.strip() if source_thread_id else None,
        source_subject=source_subject.strip() if source_subject else None,
        requested_by=requested_by.strip() if requested_by else None,
        reason=reason.strip() if reason else None,
        recorded_at=datetime.now(UTC),
    )
