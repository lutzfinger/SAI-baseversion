"""Best-effort Slack updates whenever new local-model training data is captured."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.connectors.slack import SlackConnectorError, SlackPostConnector
from app.control_plane.loaders import PolicyStore
from app.shared.config import Settings

LOGGER = logging.getLogger(__name__)

_KNOWN_BUCKETS = (
    "cloud_target",
    "operator_label_correction",
    "operator_reply_confirmed",
    "operator_outcome_failure",
)


def build_training_data_update_message(
    *,
    bucket: str,
    added_rows: Sequence[Mapping[str, Any]],
    duplicates_skipped: int,
    corpus_totals: Mapping[str, int],
) -> str:
    """Render a short Slack update for one newly appended training-data batch."""

    workflows = _workflow_counts(added_rows)
    label_counts = _label_counts(bucket=bucket, rows=added_rows)
    total_records = sum(corpus_totals.values())
    lines = [
        "New SAI training data captured.",
        f"Bucket: `{bucket}`",
        f"Added: {len(added_rows)}",
        (
            "Corpus totals: "
            f"`cloud_target` {corpus_totals.get('cloud_target', 0)}, "
            f"`operator_label_correction` {corpus_totals.get('operator_label_correction', 0)}, "
            f"`operator_reply_confirmed` {corpus_totals.get('operator_reply_confirmed', 0)}, "
            f"`operator_outcome_failure` {corpus_totals.get('operator_outcome_failure', 0)} "
            f"(total `{total_records}`)"
        ),
    ]
    if duplicates_skipped:
        lines.append(f"Duplicates skipped in this write: {duplicates_skipped}")
    if workflows:
        lines.append(f"Workflows: {_format_counter(workflows)}")
    if label_counts:
        label_heading = "Failure kinds" if bucket == "operator_outcome_failure" else "Targets"
        lines.append(f"{label_heading}: {_format_counter(label_counts)}")
    return "\n".join(lines)


def post_training_data_update(
    *,
    settings: Settings,
    bucket: str,
    rows: Sequence[Mapping[str, Any]],
    duplicates_skipped: int = 0,
) -> dict[str, str] | None:
    """Post one best-effort Slack update for newly added training-data rows."""

    if not rows:
        return None

    policy = PolicyStore(settings.policies_dir).load("slack_bot.yaml")
    connector = SlackPostConnector(
        policy=policy,
        default_channel=settings.slack_training_data_channel,
    )
    text = build_training_data_update_message(
        bucket=bucket,
        added_rows=rows,
        duplicates_skipped=duplicates_skipped,
        corpus_totals=logical_training_bucket_totals(settings),
    )
    try:
        return connector.post_message(
            text=text,
            channel=settings.slack_training_data_channel,
        )
    except SlackConnectorError:
        LOGGER.exception(
            "Failed to post training-data update for bucket %s to Slack",
            bucket,
        )
        return None


def logical_training_bucket_totals(settings: Settings) -> dict[str, int]:
    """Return deduped logical corpus counts across the stored bucket files."""

    counts: Counter[str] = Counter()
    seen_keys: set[tuple[str, str]] = set()
    for path in (
        settings.local_cloud_training_dataset_path,
        settings.local_operator_label_correction_log_path,
        settings.local_operator_outcome_log_path,
        settings.local_operator_failure_log_path,
    ):
        for row in _read_jsonl(path):
            bucket = _row_bucket(row)
            if bucket not in _KNOWN_BUCKETS:
                continue
            dedupe_key = (bucket, _row_identity(row))
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            counts[bucket] += 1
    return {bucket: counts.get(bucket, 0) for bucket in _KNOWN_BUCKETS}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _row_bucket(row: Mapping[str, Any]) -> str:
    value = str(row.get("training_target_source", "")).strip()
    if value:
        return value
    return "cloud_target" if "cloud_target" in row else ""


def _row_identity(row: Mapping[str, Any]) -> str:
    candidate = str(row.get("example_id") or row.get("outcome_id") or "").strip()
    if candidate:
        return candidate
    digest = hashlib.sha256(
        json.dumps(dict(row), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest


def _workflow_counts(rows: Sequence[Mapping[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        workflow_id = _workflow_id(row)
        if workflow_id:
            counts[workflow_id] += 1
    return counts


def _workflow_id(row: Mapping[str, Any]) -> str:
    direct = str(row.get("workflow_id", "")).strip()
    if direct:
        return direct
    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        nested = str(metadata.get("workflow_id", "")).strip()
        if nested:
            return nested
    return ""


def _label_counts(*, bucket: str, rows: Sequence[Mapping[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        if bucket == "operator_outcome_failure":
            label = str(row.get("failure_kind", "")).strip()
        else:
            label = _target_label(row)
        if label:
            counts[label] += 1
    return counts


def _target_label(row: Mapping[str, Any]) -> str:
    target = row.get("target_classification")
    if isinstance(target, Mapping):
        label = str(target.get("level2_intent", "")).strip()
        if label:
            return label
    cloud_target = row.get("cloud_target")
    if isinstance(cloud_target, Mapping):
        label = str(cloud_target.get("level2_intent", "")).strip()
        if label:
            return label
    return str(row.get("final_level2_intent", "")).strip()


def _format_counter(counter: Counter[str]) -> str:
    if not counter:
        return "(none)"
    parts = [f"`{name}` ({count})" for name, count in counter.most_common()]
    return ", ".join(parts)
