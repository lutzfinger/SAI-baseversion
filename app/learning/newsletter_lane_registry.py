"""Runtime registry for operator-approved newsletter lane rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.workers.email_models import EmailMessage

NewsletterLaneRoute = Literal["keep", "remove"]
NewsletterLaneMatchScope = Literal["sender_email", "sender_domain"]


class NewsletterLaneRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    route: NewsletterLaneRoute
    match_scope: NewsletterLaneMatchScope
    match_value: str
    source_message_reference: str
    source_message_id: str
    source_subject: str
    requested_by: str | None = None
    reason: str | None = None
    recorded_at: datetime


class NewsletterLaneRuleStore:
    """Append-only JSONL store for operator-approved newsletter routing rules."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load_rules(self) -> list[NewsletterLaneRule]:
        if not self.path.exists():
            return []
        rules: list[NewsletterLaneRule] = []
        with self.path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                rules.append(NewsletterLaneRule.model_validate(payload))
        return rules

    def match_message(self, *, message: EmailMessage) -> NewsletterLaneRule | None:
        sender = message.from_email.strip().lower()
        domain = sender.rsplit("@", 1)[1] if "@" in sender else sender
        for rule in reversed(self.load_rules()):
            if rule.match_scope == "sender_email" and sender == rule.match_value:
                return rule
            if rule.match_scope == "sender_domain" and domain == rule.match_value:
                return rule
        return None

    def record_rule(self, *, rule: NewsletterLaneRule) -> dict[str, int]:
        existing = self.load_rules()
        duplicate = next(
            (
                candidate
                for candidate in existing
                if candidate.route == rule.route
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


def build_newsletter_lane_rule(
    *,
    route: NewsletterLaneRoute,
    match_scope: NewsletterLaneMatchScope,
    match_value: str,
    source_message_reference: str,
    source_message: EmailMessage,
    requested_by: str | None,
    reason: str | None,
) -> NewsletterLaneRule:
    normalized_value = match_value.strip().lower()
    rule_id = (
        f"newsletter_lane:{route}:{match_scope}:{normalized_value}:{source_message.message_id}"
    )
    return NewsletterLaneRule(
        rule_id=rule_id,
        route=route,
        match_scope=match_scope,
        match_value=normalized_value,
        source_message_reference=source_message_reference.strip(),
        source_message_id=source_message.message_id,
        source_subject=source_message.subject,
        requested_by=requested_by.strip() if requested_by else None,
        reason=reason.strip() if reason else None,
        recorded_at=datetime.now(UTC),
    )
