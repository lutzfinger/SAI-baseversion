"""Cleanup helpers for the live local-model learning corpus."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.learning.local_cloud_comparison import (
    build_local_cloud_stats,
    parse_local_cloud_comparison_payload,
)
from app.shared.config import Settings

_SYNTHETIC_ID_PATTERN = re.compile(r"^(msg|thread|run-msg|lcx-)[-_A-Za-z0-9]*$")


class CorpusFileRewriteSummary(BaseModel):
    """One per-file rewrite summary for corpus hygiene maintenance."""

    model_config = ConfigDict(extra="forbid")

    path: str
    rows_before: int
    rows_after: int
    synthetic_rows_quarantined: int = 0
    backup_path: str | None = None
    quarantine_path: str | None = None


class CorpusCleanupSummary(BaseModel):
    """Operator-facing result of one corpus rebuild pass."""

    model_config = ConfigDict(extra="forbid")

    executed_at: datetime
    quarantine_dir: str
    cloud_target_rows_rebuilt: int
    operator_reply_confirmed_rows_kept: int
    operator_label_correction_rows_kept: int
    extra_training_rows_preserved: int
    comparison_rows_kept: int
    operator_outcome_failure_rows_kept: int
    active_training_rows_before: int
    active_training_rows_after: int
    training_state_reserved_ids_removed: int = 0
    files: dict[str, CorpusFileRewriteSummary] = Field(default_factory=dict)


def rebuild_learning_corpus(
    *,
    settings: Settings,
    executed_at: datetime | None = None,
) -> CorpusCleanupSummary:
    """Rebuild the active training corpus and quarantine synthetic rows."""

    timestamp = executed_at or datetime.now(UTC)
    quarantine_dir = (
        settings.learning_dir
        / "quarantine"
        / f"corpus_cleanup_{timestamp.strftime('%Y%m%dT%H%M%SZ')}"
    )
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    comparison_rows = _read_jsonl(settings.local_cloud_comparison_log_path)
    clean_comparisons, synthetic_comparisons = _partition_synthetic_rows(comparison_rows)
    parsed_comparisons = [
        parse_local_cloud_comparison_payload(payload)
        for payload in clean_comparisons
    ]
    rebuilt_cloud_target_rows = [
        example.model_dump(mode="json")
        for example in parsed_comparisons
        if example.disagreement.overall_disagreement
    ]

    operator_reply_rows = _read_jsonl(settings.local_operator_outcome_log_path)
    clean_operator_reply_rows, synthetic_operator_reply_rows = _partition_synthetic_rows(
        operator_reply_rows
    )

    operator_failure_rows = _read_jsonl(settings.local_operator_failure_log_path)
    clean_operator_failure_rows, synthetic_operator_failure_rows = _partition_synthetic_rows(
        operator_failure_rows
    )

    operator_label_rows = _read_jsonl(settings.local_operator_label_correction_log_path)
    clean_operator_label_rows, synthetic_operator_label_rows = _partition_synthetic_rows(
        operator_label_rows
    )

    current_training_rows = _read_jsonl(settings.local_cloud_training_dataset_path)
    clean_current_training_rows, synthetic_training_rows = _partition_synthetic_rows(
        current_training_rows
    )
    rebuilt_bucket_ids = {
        _row_identity(row)
        for row in (
            rebuilt_cloud_target_rows
            + clean_operator_reply_rows
            + clean_operator_label_rows
        )
    }
    preserved_extra_training_rows = [
        row
        for row in clean_current_training_rows
        if str(row.get("training_target_source", "")).strip()
        not in {"cloud_target", "operator_reply_confirmed", "operator_label_correction"}
        and _row_identity(row) not in rebuilt_bucket_ids
    ]

    active_training_rows = _dedupe_rows(
        rebuilt_cloud_target_rows
        + clean_operator_label_rows
        + clean_operator_reply_rows
        + preserved_extra_training_rows
    )

    files: dict[str, CorpusFileRewriteSummary] = {}
    files["local_cloud_comparisons"] = _rewrite_with_quarantine(
        path=settings.local_cloud_comparison_log_path,
        kept_rows=clean_comparisons,
        synthetic_rows=synthetic_comparisons,
        quarantine_dir=quarantine_dir,
    )
    files["local_operator_outcomes"] = _rewrite_with_quarantine(
        path=settings.local_operator_outcome_log_path,
        kept_rows=clean_operator_reply_rows,
        synthetic_rows=synthetic_operator_reply_rows,
        quarantine_dir=quarantine_dir,
    )
    files["local_operator_outcome_failures"] = _rewrite_with_quarantine(
        path=settings.local_operator_failure_log_path,
        kept_rows=clean_operator_failure_rows,
        synthetic_rows=synthetic_operator_failure_rows,
        quarantine_dir=quarantine_dir,
    )
    files["local_operator_label_corrections"] = _rewrite_with_quarantine(
        path=settings.local_operator_label_correction_log_path,
        kept_rows=clean_operator_label_rows,
        synthetic_rows=synthetic_operator_label_rows,
        quarantine_dir=quarantine_dir,
    )
    files["local_cloud_training_dataset"] = _rewrite_with_quarantine(
        path=settings.local_cloud_training_dataset_path,
        kept_rows=active_training_rows,
        synthetic_rows=synthetic_training_rows,
        quarantine_dir=quarantine_dir,
    )

    settings.local_cloud_stats_path.write_text(
        json.dumps(
            build_local_cloud_stats(examples=parsed_comparisons, updated_at=timestamp),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    training_state_reserved_ids_removed = _rewrite_training_state(
        path=settings.local_cloud_training_state_path,
        valid_example_ids={_row_identity(row) for row in active_training_rows},
    )

    manifest = {
        "executed_at": timestamp.isoformat(),
        "quarantine_dir": str(quarantine_dir),
        "cloud_target_rows_rebuilt": len(rebuilt_cloud_target_rows),
        "operator_reply_confirmed_rows_kept": len(clean_operator_reply_rows),
        "operator_label_correction_rows_kept": len(clean_operator_label_rows),
        "extra_training_rows_preserved": len(preserved_extra_training_rows),
        "comparison_rows_kept": len(clean_comparisons),
        "operator_outcome_failure_rows_kept": len(clean_operator_failure_rows),
        "active_training_rows_before": len(current_training_rows),
        "active_training_rows_after": len(active_training_rows),
        "training_state_reserved_ids_removed": training_state_reserved_ids_removed,
        "files": {
            key: value.model_dump(mode="json")
            for key, value in files.items()
        },
    }
    (quarantine_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    return CorpusCleanupSummary(
        executed_at=timestamp,
        quarantine_dir=str(quarantine_dir),
        cloud_target_rows_rebuilt=len(rebuilt_cloud_target_rows),
        operator_reply_confirmed_rows_kept=len(clean_operator_reply_rows),
        operator_label_correction_rows_kept=len(clean_operator_label_rows),
        extra_training_rows_preserved=len(preserved_extra_training_rows),
        comparison_rows_kept=len(clean_comparisons),
        operator_outcome_failure_rows_kept=len(clean_operator_failure_rows),
        active_training_rows_before=len(current_training_rows),
        active_training_rows_after=len(active_training_rows),
        training_state_reserved_ids_removed=training_state_reserved_ids_removed,
        files=files,
    )


def is_synthetic_learning_row(row: Mapping[str, Any]) -> bool:
    """Return true when a row clearly comes from fixtures or synthetic tests."""

    message = _message_payload(row)
    metadata = row.get("metadata")
    metadata_map = metadata if isinstance(metadata, Mapping) else {}

    for raw_value in (
        row.get("example_id"),
        row.get("outcome_id"),
        row.get("run_id"),
        metadata_map.get("run_id"),
        metadata_map.get("source_example_id"),
        message.get("message_id"),
        message.get("thread_id"),
    ):
        if _looks_like_synthetic_identifier(raw_value):
            return True

    from_email = str(message.get("from_email", "")).strip().lower()
    if from_email.endswith("@example.com"):
        return True

    for raw_text in (
        message.get("subject"),
        message.get("snippet"),
        message.get("body_excerpt"),
    ):
        text = str(raw_text or "").strip()
        if not text:
            continue
        if text.startswith(("Subject msg-", "Snippet msg-", "Body msg-")):
            return True
    return False


def _message_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    message = row.get("message")
    if isinstance(message, Mapping):
        return message
    input_email = row.get("input_email")
    if isinstance(input_email, Mapping):
        return input_email
    return {}


def _looks_like_synthetic_identifier(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(_SYNTHETIC_ID_PATTERN.match(text))


def _partition_synthetic_rows(
    rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for row in rows:
        if is_synthetic_learning_row(row):
            quarantined.append(row)
        else:
            kept.append(row)
    return kept, quarantined


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), sort_keys=True))
            handle.write("\n")


def _rewrite_with_quarantine(
    *,
    path: Path,
    kept_rows: Sequence[Mapping[str, Any]],
    synthetic_rows: Sequence[Mapping[str, Any]],
    quarantine_dir: Path,
) -> CorpusFileRewriteSummary:
    rows_before = len(_read_jsonl(path))
    backup_path: str | None = None
    if path.exists():
        backup_file = quarantine_dir / f"{path.name}.before"
        shutil.copy2(path, backup_file)
        backup_path = str(backup_file)

    if kept_rows or path.exists():
        _write_jsonl(path, kept_rows)

    quarantine_path: str | None = None
    if synthetic_rows:
        quarantine_file = quarantine_dir / f"{path.name}.synthetic_rows"
        _write_jsonl(quarantine_file, synthetic_rows)
        quarantine_path = str(quarantine_file)

    return CorpusFileRewriteSummary(
        path=str(path),
        rows_before=rows_before,
        rows_after=len(kept_rows),
        synthetic_rows_quarantined=len(synthetic_rows),
        backup_path=backup_path,
        quarantine_path=quarantine_path,
    )


def _row_identity(row: Mapping[str, Any]) -> str:
    candidate = str(row.get("example_id") or row.get("outcome_id") or "").strip()
    if candidate:
        return candidate
    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        candidate = str(metadata.get("source_example_id") or "").strip()
        if candidate:
            return candidate
    message = _message_payload(row)
    message_id = str(message.get("message_id") or "").strip()
    if message_id:
        return message_id
    return json.dumps(dict(row), sort_keys=True, separators=(",", ":"))


def _dedupe_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        identity = _row_identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(dict(row))
    return deduped


def _rewrite_training_state(*, path: Path, valid_example_ids: set[str]) -> int:
    if not path.exists():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    removed = 0
    for section in ("local_prompt_tuning", "local_lora_fine_tune"):
        stage = payload.get(section)
        if not isinstance(stage, dict):
            continue
        reserved = stage.get("reserved_example_ids", [])
        if not isinstance(reserved, list):
            continue
        kept = [example_id for example_id in reserved if str(example_id) in valid_example_ids]
        removed += len(reserved) - len(kept)
        stage["reserved_example_ids"] = kept
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return removed
