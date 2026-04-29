"""Mine Lutz's real mailbox behavior into local-model learning signals."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.learning.local_cloud_comparison import (
    LocalCloudComparisonExample,
    parse_local_cloud_comparison_payload,
)
from app.learning.training_data_notifier import post_training_data_update
from app.learning.training_pipeline import LocalModelTrainingRecord
from app.shared.config import Settings

LOGGER = logging.getLogger(__name__)

REACTION_REQUIRED_LEVEL2 = {
    "meeting_request",
    "decision_approval",
    "action_required",
    "ask_for_help_advice",
}


class SentReplyObservation(BaseModel):
    """Minimal sent-message observation used to confirm operator action."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    thread_id: str
    sent_at: datetime | None = None


class OperatorOutcomeFailureRecord(BaseModel):
    """Negative supervision hint derived from missing or unexpected replies."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    outcome_id: str
    observed_at: datetime
    source_example_id: str
    source_message_id: str
    source_thread_id: str | None = None
    failure_kind: str
    final_level1_classification: str
    final_level2_intent: str
    training_target_source: str = "operator_outcome_failure"
    age_days: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperatorOutcomeRefreshSummary(BaseModel):
    """Operator-facing summary of one outcome-refresh pass."""

    model_config = ConfigDict(extra="forbid")

    comparison_examples_seen: int = 0
    sent_replies_seen: int = 0
    reply_confirmed_records: int = 0
    reply_confirmed_duplicates: int = 0
    missing_reply_failures: int = 0
    missing_reply_duplicates: int = 0
    unexpected_reply_failures: int = 0
    unexpected_reply_duplicates: int = 0


class OperatorOutcomeRecorder:
    """Append outcome-derived learning signals without duplicating prior rows."""

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings

    def record_reply_confirmations(
        self,
        *,
        records: list[LocalModelTrainingRecord],
    ) -> dict[str, int]:
        if not records:
            return {"received": 0, "recorded": 0, "duplicates_skipped": 0}
        existing_ids = self._existing_ids(
            paths=(
                self.settings.local_cloud_training_dataset_path,
                self.settings.local_operator_outcome_log_path,
            )
        )
        rows_to_append: list[LocalModelTrainingRecord] = []
        duplicates = 0
        for record in records:
            if record.example_id in existing_ids:
                duplicates += 1
                continue
            rows_to_append.append(record)
            existing_ids.add(record.example_id)
        if not rows_to_append:
            return {"received": len(records), "recorded": 0, "duplicates_skipped": duplicates}

        self._append_jsonl(
            self.settings.local_cloud_training_dataset_path,
            [record.model_dump(mode="json") for record in rows_to_append],
        )
        self._append_jsonl(
            self.settings.local_operator_outcome_log_path,
            [record.model_dump(mode="json") for record in rows_to_append],
        )
        post_training_data_update(
            settings=self.settings,
            bucket="operator_reply_confirmed",
            rows=[record.model_dump(mode="json") for record in rows_to_append],
            duplicates_skipped=duplicates,
        )
        return {
            "received": len(records),
            "recorded": len(rows_to_append),
            "duplicates_skipped": duplicates,
        }

    def record_failures(
        self,
        *,
        failures: list[OperatorOutcomeFailureRecord],
    ) -> dict[str, int]:
        if not failures:
            return {"received": 0, "recorded": 0, "duplicates_skipped": 0}
        existing_ids = self._existing_ids(paths=(self.settings.local_operator_failure_log_path,))
        rows_to_append: list[OperatorOutcomeFailureRecord] = []
        duplicates = 0
        for failure in failures:
            if failure.outcome_id in existing_ids:
                duplicates += 1
                continue
            rows_to_append.append(failure)
            existing_ids.add(failure.outcome_id)
        if not rows_to_append:
            return {"received": len(failures), "recorded": 0, "duplicates_skipped": duplicates}

        self._append_jsonl(
            self.settings.local_operator_failure_log_path,
            [failure.model_dump(mode="json") for failure in rows_to_append],
        )
        post_training_data_update(
            settings=self.settings,
            bucket="operator_outcome_failure",
            rows=[failure.model_dump(mode="json") for failure in rows_to_append],
            duplicates_skipped=duplicates,
        )
        return {
            "received": len(failures),
            "recorded": len(rows_to_append),
            "duplicates_skipped": duplicates,
        }

    def _existing_ids(self, *, paths: Iterable[Path]) -> set[str]:
        ids: set[str] = set()
        for path in paths:
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    candidate = str(
                        payload.get("example_id") or payload.get("outcome_id") or ""
                    ).strip()
                    if candidate:
                        ids.add(candidate)
        return ids

    def _append_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True))
                handle.write("\n")


def refresh_operator_outcomes_from_service(
    *,
    settings: Settings,
    service: Any,
    user_id: str = "me",
    max_sent_messages: int | None = None,
    failure_lookback_days: int | None = None,
) -> OperatorOutcomeRefreshSummary:
    """Augment the training corpus with operator-outcome signals."""

    comparison_examples = _load_comparison_examples(settings.local_cloud_comparison_log_path)
    stale_after_days = (
        failure_lookback_days or settings.local_operator_outcome_failure_lookback_days
    )
    sent_replies = _fetch_sent_replies(
        service=service,
        user_id=user_id,
        max_sent_messages=max_sent_messages or settings.local_operator_outcome_reply_scan_limit,
        lookback_days=stale_after_days,
        sai_alias_email=settings.sai_alias_email,
    )
    replies_by_thread = _reply_map(sent_replies)
    now = datetime.now(UTC)

    confirmed_records: list[LocalModelTrainingRecord] = []
    missing_reply_failures: list[OperatorOutcomeFailureRecord] = []
    unexpected_reply_failures: list[OperatorOutcomeFailureRecord] = []

    for example in comparison_examples:
        thread_id = example.message.thread_id
        if not thread_id:
            continue
        reply = replies_by_thread.get(thread_id)
        has_reply = (
            reply is not None
            and reply.sent_at is not None
            and example.message.received_at is not None
            and reply.sent_at > example.message.received_at
        )
        age_days = _age_days(example.message.received_at, now=now)
        level2 = example.final_classification.level2_intent

        if level2 in REACTION_REQUIRED_LEVEL2 and has_reply and reply is not None:
            confirmed_records.append(_reply_confirmed_record(example=example, reply=reply))
            continue
        if (
            level2 in REACTION_REQUIRED_LEVEL2
            and age_days is not None
            and age_days >= stale_after_days
        ):
            missing_reply_failures.append(
                _failure_record(
                    example=example,
                    failure_kind="missing_reply_for_response_needed_classification",
                    age_days=age_days,
                )
            )
            continue
        if level2 not in REACTION_REQUIRED_LEVEL2 and has_reply:
            unexpected_reply_failures.append(
                _failure_record(
                    example=example,
                    failure_kind="unexpected_reply_for_non_reaction_classification",
                    age_days=age_days,
                    reply=reply,
                )
            )

    recorder = OperatorOutcomeRecorder(settings=settings)
    confirmed_summary = recorder.record_reply_confirmations(records=confirmed_records)
    missing_summary = recorder.record_failures(failures=missing_reply_failures)
    unexpected_summary = recorder.record_failures(failures=unexpected_reply_failures)

    return OperatorOutcomeRefreshSummary(
        comparison_examples_seen=len(comparison_examples),
        sent_replies_seen=len(sent_replies),
        reply_confirmed_records=confirmed_summary["recorded"],
        reply_confirmed_duplicates=confirmed_summary["duplicates_skipped"],
        missing_reply_failures=missing_summary["recorded"],
        missing_reply_duplicates=missing_summary["duplicates_skipped"],
        unexpected_reply_failures=unexpected_summary["recorded"],
        unexpected_reply_duplicates=unexpected_summary["duplicates_skipped"],
    )


def refresh_operator_outcomes_best_effort(
    *,
    settings: Settings,
    service: Any,
    user_id: str = "me",
    max_sent_messages: int | None = None,
    failure_lookback_days: int | None = None,
) -> OperatorOutcomeRefreshSummary | None:
    """Best-effort wrapper that never breaks the caller."""

    try:
        return refresh_operator_outcomes_from_service(
            settings=settings,
            service=service,
            user_id=user_id,
            max_sent_messages=max_sent_messages,
            failure_lookback_days=failure_lookback_days,
        )
    except Exception:  # pragma: no cover - best effort by design
        LOGGER.exception("Failed to refresh operator-outcome learning signals.")
        return None


def _load_comparison_examples(path: Path) -> list[LocalCloudComparisonExample]:
    if not path.exists():
        return []
    by_example_id: dict[str, LocalCloudComparisonExample] = {}
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            example = parse_local_cloud_comparison_payload(json.loads(line))
            by_example_id.setdefault(example.example_id, example)
    return list(by_example_id.values())


def _fetch_sent_replies(
    *,
    service: Any,
    user_id: str,
    max_sent_messages: int,
    lookback_days: int,
    sai_alias_email: str,
) -> list[SentReplyObservation]:
    query = f'in:sent newer_than:{lookback_days}d -from:{sai_alias_email}'
    response = (
        service.users()
        .messages()
        .list(
            userId=user_id,
            q=query,
            maxResults=max_sent_messages,
            includeSpamTrash=False,
        )
        .execute()
    )
    refs = response.get("messages", [])
    if not isinstance(refs, list):
        return []
    observations: list[SentReplyObservation] = []
    for item in refs:
        if not isinstance(item, dict):
            continue
        message_id = str(item.get("id", "")).strip()
        if not message_id:
            continue
        payload = (
            service.users()
            .messages()
            .get(userId=user_id, id=message_id, format="full")
            .execute()
        )
        thread_id = str(payload.get("threadId", "")).strip()
        if not thread_id:
            continue
        observations.append(
            SentReplyObservation(
                message_id=message_id,
                thread_id=thread_id,
                sent_at=_parse_internal_date(payload.get("internalDate")),
            )
        )
    return observations


def _reply_map(observations: list[SentReplyObservation]) -> dict[str, SentReplyObservation]:
    replies: dict[str, SentReplyObservation] = {}
    for observation in observations:
        current = replies.get(observation.thread_id)
        if current is None:
            replies[observation.thread_id] = observation
            continue
        if current.sent_at is None or (
            observation.sent_at is not None and observation.sent_at < current.sent_at
        ):
            replies[observation.thread_id] = observation
    return replies


def _reply_confirmed_record(
    *,
    example: LocalCloudComparisonExample,
    reply: SentReplyObservation,
) -> LocalModelTrainingRecord:
    return LocalModelTrainingRecord(
        example_id=_operator_outcome_id(
            prefix="opr",
            source_example_id=example.example_id,
            detail=reply.message_id,
        ),
        input_email=example.message.model_dump(mode="json"),
        target_classification=example.final_classification.model_dump(mode="json"),
        prior_local_prediction=example.local_prediction.model_dump(mode="json"),
        keyword_baseline=(
            example.keyword_baseline.model_dump(mode="json")
            if example.keyword_baseline is not None
            else None
        ),
        training_target_source="operator_reply_confirmed",
        metadata={
            "workflow_id": example.workflow_id,
            "run_id": example.run_id,
            "captured_at": example.captured_at.isoformat(),
            "source_kind": "operator_reply_confirmed",
            "source_example_id": example.example_id,
            "reply_message_id": reply.message_id,
            "reply_sent_at": reply.sent_at.isoformat() if reply.sent_at else None,
            "final_classification": example.final_classification.model_dump(mode="json"),
            "cloud_target": example.cloud_target.model_dump(mode="json"),
        },
    )


def _failure_record(
    *,
    example: LocalCloudComparisonExample,
    failure_kind: str,
    age_days: float | None,
    reply: SentReplyObservation | None = None,
) -> OperatorOutcomeFailureRecord:
    return OperatorOutcomeFailureRecord(
        outcome_id=_operator_outcome_id(
            prefix="opf",
            source_example_id=example.example_id,
            detail=failure_kind,
        ),
        observed_at=datetime.now(UTC),
        source_example_id=example.example_id,
        source_message_id=example.message.message_id,
        source_thread_id=example.message.thread_id,
        failure_kind=failure_kind,
        final_level1_classification=example.final_classification.level1_classification,
        final_level2_intent=example.final_classification.level2_intent,
        age_days=age_days,
        metadata={
            "workflow_id": example.workflow_id,
            "run_id": example.run_id,
            "reply_message_id": reply.message_id if reply is not None else None,
            "reply_sent_at": reply.sent_at.isoformat() if reply and reply.sent_at else None,
        },
    )


def _operator_outcome_id(*, prefix: str, source_example_id: str, detail: str) -> str:
    digest = hashlib.sha256(f"{source_example_id}:{detail}".encode()).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _parse_internal_date(raw_value: object) -> datetime | None:
    if raw_value is None:
        return None
    try:
        millis = int(str(raw_value))
    except ValueError:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def _age_days(received_at: datetime | None, *, now: datetime) -> float | None:
    if received_at is None:
        return None
    delta = now - received_at
    return max(delta, timedelta()).total_seconds() / 86400
