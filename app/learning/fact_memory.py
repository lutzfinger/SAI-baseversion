"""General timestamped fact memory for SAI."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FactMemoryRecord(BaseModel):
    """One timestamped fact SAI may reuse across workflows."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    fact_key: str
    value: str
    source_kind: str
    source_workflow_id: str
    source_run_id: str
    source_reference: str
    source_thread_id: str | None = None
    source_message_id: str | None = None
    observed_at: datetime
    recorded_at: datetime
    confidence: float
    scope: str = "global"
    access_rules: list[str] = Field(default_factory=list)
    version: int = 1
    status: str = "active"
    supersedes_fact_id: str | None = None
    sensitive: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class FactMemoryStore:
    """SQLite-backed general fact memory with simple scoped retrieval."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fact_memory (
                    fact_id TEXT PRIMARY KEY,
                    fact_key TEXT NOT NULL,
                    value_text TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_workflow_id TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    source_reference TEXT NOT NULL,
                    source_thread_id TEXT,
                    source_message_id TEXT,
                    observed_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    scope TEXT NOT NULL,
                    access_rules_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active',
                    supersedes_fact_id TEXT,
                    sensitive INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_fact_memory_recorded_at
                ON fact_memory(recorded_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_fact_memory_fact_key
                ON fact_memory(fact_key, recorded_at DESC)
                """
            )
            self._ensure_column(
                connection,
                table_name="fact_memory",
                column_name="version",
                definition="INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                connection,
                table_name="fact_memory",
                column_name="status",
                definition="TEXT NOT NULL DEFAULT 'active'",
            )
            self._ensure_column(
                connection,
                table_name="fact_memory",
                column_name="supersedes_fact_id",
                definition="TEXT",
            )
            self._ensure_column(
                connection,
                table_name="fact_memory",
                column_name="sensitive",
                definition="INTEGER NOT NULL DEFAULT 0",
            )
            connection.commit()

    def record_facts(self, records: list[FactMemoryRecord]) -> int:
        """Append newly observed facts."""

        if not records:
            return 0
        with self._connect() as connection:
            inserted = 0
            for record in records:
                resolved = self._resolve_fact_version(connection=connection, record=record)
                before_changes = connection.total_changes
                connection.execute(
                    """
                    INSERT OR IGNORE INTO fact_memory (
                        fact_id,
                        fact_key,
                        value_text,
                        source_kind,
                        source_workflow_id,
                        source_run_id,
                        source_reference,
                        source_thread_id,
                        source_message_id,
                        observed_at,
                        recorded_at,
                        confidence,
                        scope,
                        access_rules_json,
                        version,
                        status,
                        supersedes_fact_id,
                        sensitive,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved.fact_id,
                        resolved.fact_key,
                        resolved.value,
                        resolved.source_kind,
                        resolved.source_workflow_id,
                        resolved.source_run_id,
                        resolved.source_reference,
                        resolved.source_thread_id,
                        resolved.source_message_id,
                        resolved.observed_at.isoformat(),
                        resolved.recorded_at.isoformat(),
                        resolved.confidence,
                        resolved.scope,
                        json.dumps(resolved.access_rules, sort_keys=True),
                        resolved.version,
                        resolved.status,
                        resolved.supersedes_fact_id,
                        1 if resolved.sensitive else 0,
                        json.dumps(resolved.metadata, sort_keys=True),
                    ),
                )
                inserted += int(connection.total_changes > before_changes)
            connection.commit()
        return inserted

    def list_recent_facts(self, *, limit: int = 20) -> list[FactMemoryRecord]:
        """Return recent facts for inspection."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM fact_memory
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_fact(row) for row in rows]

    def list_fact_history(self, *, fact_key: str) -> list[FactMemoryRecord]:
        """Return versioned history for one fact key."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM fact_memory
                WHERE fact_key = ?
                ORDER BY version ASC, recorded_at ASC
                """,
                (fact_key,),
            ).fetchall()
        return [_row_to_fact(row) for row in rows]

    def query_relevant_facts(
        self,
        *,
        query_text: str,
        workflow_id: str,
        limit: int = 5,
    ) -> list[FactMemoryRecord]:
        """Return a small set of likely-relevant latest facts for one workflow."""

        latest_by_key = self._latest_active_facts(workflow_id=workflow_id)
        query_tokens = _tokenize(query_text)
        scored: list[tuple[float, FactMemoryRecord]] = []
        for record in latest_by_key.values():
            score = _fact_score(record=record, query_tokens=query_tokens)
            if score <= 0:
                continue
            scored.append((score, record))
        scored.sort(key=lambda item: (item[0], item[1].recorded_at), reverse=True)
        return [record for _, record in scored[:limit]]

    def _latest_active_facts(self, *, workflow_id: str) -> dict[str, FactMemoryRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM fact_memory
                WHERE status = 'active'
                ORDER BY recorded_at DESC
                """
            ).fetchall()
        latest_by_key: dict[str, FactMemoryRecord] = {}
        for row in rows:
            record = _row_to_fact(row)
            if record.fact_key in latest_by_key:
                continue
            if not _is_fact_accessible(record=record, workflow_id=workflow_id):
                continue
            latest_by_key[record.fact_key] = record
        return latest_by_key

    def _resolve_fact_version(
        self,
        *,
        connection: sqlite3.Connection,
        record: FactMemoryRecord,
    ) -> FactMemoryRecord:
        previous_row = connection.execute(
            """
            SELECT *
            FROM fact_memory
            WHERE fact_key = ? AND status = 'active'
            ORDER BY version DESC, recorded_at DESC
            LIMIT 1
            """,
            (record.fact_key,),
        ).fetchone()
        if previous_row is None:
            return record
        previous = _row_to_fact(previous_row)
        if previous.value.strip().lower() == record.value.strip().lower():
            return record.model_copy(
                update={
                    "version": previous.version,
                    "supersedes_fact_id": previous.supersedes_fact_id,
                    "status": previous.status,
                    "sensitive": previous.sensitive or record.sensitive,
                }
            )
        connection.execute(
            """
            UPDATE fact_memory
            SET status = 'superseded'
            WHERE fact_id = ?
            """,
            (previous.fact_id,),
        )
        return record.model_copy(
            update={
                "version": previous.version + 1,
                "supersedes_fact_id": previous.fact_id,
            }
        )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        *,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row[1]) for row in rows}
        if column_name in existing:
            return
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def extract_operator_facts(
    *,
    text: str,
    source_workflow_id: str,
    source_run_id: str,
    source_reference: str,
    source_thread_id: str | None,
    source_message_id: str | None,
) -> list[FactMemoryRecord]:
    """Extract explicit factual statements from operator-authored text."""

    observed_at = datetime.now(UTC)
    text_value = text.strip()
    if not text_value:
        return []
    records: list[FactMemoryRecord] = []
    seen_keys: set[tuple[str, str]] = set()
    for pattern in _FACT_PATTERNS:
        for match in pattern["regex"].finditer(text_value):
            value = _normalize_fact_value(match.group("value"))
            if not value:
                continue
            fact_key = str(pattern["fact_key"])
            dedupe_key = (fact_key, value.lower())
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            records.append(
                FactMemoryRecord(
                    fact_id=(
                        f"fact:{source_workflow_id}:{source_message_id or source_reference}:"
                        f"{fact_key}:{value.lower()}"
                    ),
                    fact_key=fact_key,
                    value=value,
                    source_kind="operator_email",
                    source_workflow_id=source_workflow_id,
                    source_run_id=source_run_id,
                    source_reference=source_reference,
                    source_thread_id=source_thread_id,
                    source_message_id=source_message_id,
                    observed_at=observed_at,
                    recorded_at=observed_at,
                    confidence=float(pattern["confidence"]),
                    scope=str(pattern["scope"]),
                    access_rules=list(pattern["access_rules"]),
                    sensitive=bool(pattern.get("sensitive", False)),
                    metadata={
                        "aliases": list(pattern["aliases"]),
                        "conflict_strategy": str(
                            pattern.get("conflict_strategy", "replace_latest")
                        ),
                    },
                )
            )
    return records


def render_fact_context(records: list[FactMemoryRecord]) -> list[dict[str, Any]]:
    """Render compact JSON-friendly fact snippets for planner context."""

    return [
        {
            "fact_key": record.fact_key,
            "value": record.value,
            "version": record.version,
            "status": record.status,
            "observed_at": record.observed_at.isoformat(),
            "confidence": record.confidence,
            "scope": record.scope,
            "source_reference": record.source_reference,
            "sensitive": record.sensitive,
        }
        for record in records
    ]


_FACT_PATTERNS: tuple[dict[str, Any], ...] = (
    {
        "fact_key": "home_address",
        "regex": re.compile(
            r"\bmy\s+home(?:\s+address)?\s+is\s+(?P<value>[^.\n]+)",
            flags=re.IGNORECASE,
        ),
        "confidence": 0.99,
        "scope": "personal_sensitive",
        "access_rules": ("workflow:sai-email-interaction",),
        "sensitive": True,
        "conflict_strategy": "replace_latest",
        "aliases": ("home", "address", "live", "house"),
    },
    {
        "fact_key": "home_address",
        "regex": re.compile(
            r"\bi\s+live\s+at\s+(?P<value>[^.\n]+)",
            flags=re.IGNORECASE,
        ),
        "confidence": 0.99,
        "scope": "personal_sensitive",
        "access_rules": ("workflow:sai-email-interaction",),
        "sensitive": True,
        "conflict_strategy": "replace_latest",
        "aliases": ("home", "address", "live", "house"),
    },
    {
        "fact_key": "teaching_location",
        "regex": re.compile(
            r"\bi\s+(?:am\s+)?teach(?:ing)?\s+in\s+(?P<value>[^.\n]+)",
            flags=re.IGNORECASE,
        ),
        "confidence": 0.96,
        "scope": "teaching",
        "access_rules": ("workflow:sai-email-interaction",),
        "sensitive": False,
        "conflict_strategy": "replace_latest",
        "aliases": ("teach", "teaching", "class", "sage", "ithaca", "hall"),
    },
)


def _row_to_fact(row: sqlite3.Row) -> FactMemoryRecord:
    return FactMemoryRecord(
        fact_id=str(row["fact_id"]),
        fact_key=str(row["fact_key"]),
        value=str(row["value_text"]),
        source_kind=str(row["source_kind"]),
        source_workflow_id=str(row["source_workflow_id"]),
        source_run_id=str(row["source_run_id"]),
        source_reference=str(row["source_reference"]),
        source_thread_id=(
            str(row["source_thread_id"]) if row["source_thread_id"] is not None else None
        ),
        source_message_id=(
            str(row["source_message_id"]) if row["source_message_id"] is not None else None
        ),
        observed_at=datetime.fromisoformat(str(row["observed_at"])),
        recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
        confidence=float(row["confidence"]),
        scope=str(row["scope"]),
        access_rules=json.loads(str(row["access_rules_json"])),
        version=int(row["version"]) if row["version"] is not None else 1,
        status=str(row["status"]) if row["status"] is not None else "active",
        supersedes_fact_id=(
            str(row["supersedes_fact_id"]) if row["supersedes_fact_id"] is not None else None
        ),
        sensitive=bool(row["sensitive"]) if row["sensitive"] is not None else False,
        metadata=json.loads(str(row["metadata_json"])),
    )


def _is_fact_accessible(*, record: FactMemoryRecord, workflow_id: str) -> bool:
    rules = {rule.strip() for rule in record.access_rules if rule.strip()}
    if not rules:
        return False
    return "workflow:*" in rules or f"workflow:{workflow_id}" in rules


def _fact_score(*, record: FactMemoryRecord, query_tokens: set[str]) -> float:
    haystack_tokens = _tokenize(
        " ".join(
            [
                record.fact_key,
                record.value,
                record.scope,
                " ".join(str(alias) for alias in record.metadata.get("aliases", [])),
            ]
        )
    )
    overlap = len(query_tokens & haystack_tokens)
    if overlap > 0:
        return float(overlap) + record.confidence
    if query_tokens & {
        "flight",
        "travel",
        "airport",
        "calendar",
        "meeting",
    } and record.fact_key in {"home_address", "teaching_location"}:
        return 0.5 + record.confidence
    return 0.0


def _normalize_fact_value(value: str) -> str:
    return " ".join(value.strip().strip("\"'").split())


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 1}
