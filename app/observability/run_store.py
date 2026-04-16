"""SQLite-backed operational state for runs, approvals, and reflection.

The original plan asked for SQLite initially with a clean upgrade path. This
module keeps the relational state intentionally simple so it remains easy to
inspect manually and easy to migrate later if the project grows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from app.approvals.models import ApprovalRequest
from app.control_plane.slack_models import (
    SlackFeedbackRecord,
    SlackFeedbackType,
    SlackQuestionRecord,
    SlackQuestionStatus,
)
from app.observability.task_plane_models import TaskEventRecord, TaskRecord, TaskStepRecord
from app.reflection.models import ReflectionReport
from app.shared.models import RunRecord, RunStatus


class RunStore:
    """Persist lightweight structured state for the control plane."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection with row access by column name."""

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        """Create the small set of tables the first version needs."""

        with self._connect() as connection:
            # `runs` is the dashboard-friendly summary table for workflow runs.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                )
                """
            )
            # `approvals` preserves human decisions independently of run output.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    request_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT,
                    status TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            # `reflection_reports` stores suggestion-only review output.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reflection_reports (
                    report_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    findings_json TEXT NOT NULL,
                    source_run_ids_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_items (
                    workflow_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY (workflow_id, item_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_thread_id TEXT,
                    source_message_id TEXT,
                    requested_by TEXT,
                    title TEXT NOT NULL,
                    task_kind TEXT,
                    status TEXT NOT NULL,
                    current_plan_json TEXT NOT NULL,
                    pending_question TEXT,
                    approval_request_ids_json TEXT NOT NULL,
                    linked_thread_ids_json TEXT NOT NULL,
                    linked_message_ids_json TEXT NOT NULL,
                    opaque_payload_json TEXT NOT NULL,
                    last_run_id TEXT,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_source_message
                ON tasks(source_message_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_source_thread
                ON tasks(source_thread_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_updated_at
                ON tasks(updated_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_steps (
                    task_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    step_kind TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approval_required INTEGER NOT NULL,
                    sequence_number INTEGER,
                    payload_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (task_id, step_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_steps_task
                ON task_steps(task_id, sequence_number, updated_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_step_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    step_kind TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approval_required INTEGER NOT NULL,
                    sequence_number INTEGER,
                    payload_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_step_events_task
                ON task_step_events(task_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    step_id TEXT,
                    step_kind TEXT,
                    status TEXT,
                    sequence_number INTEGER,
                    payload_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_events_task
                ON task_events(task_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS slack_questions (
                    question_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    run_id TEXT,
                    item_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_slack_questions_channel_thread
                ON slack_questions(channel_id, thread_ts)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS slack_feedback (
                    feedback_id TEXT PRIMARY KEY,
                    question_id TEXT,
                    workflow_id TEXT,
                    run_id TEXT,
                    item_id TEXT,
                    slack_user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    message_ts TEXT NOT NULL,
                    feedback_type TEXT NOT NULL,
                    text TEXT,
                    action_id TEXT,
                    value TEXT,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def create_run(self, record: RunRecord) -> None:
        """Insert a new run row before workflow execution begins."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (run_id, workflow_id, status, started_at, updated_at, summary_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.workflow_id,
                    record.status.value,
                    record.started_at.isoformat(),
                    record.updated_at.isoformat(),
                    json.dumps(record.summary, sort_keys=True),
                ),
            )
            connection.commit()

    def update_run_status(
        self, run_id: str, status: RunStatus, summary: dict[str, Any] | None = None
    ) -> RunRecord:
        """Update run status while preserving the latest structured summary."""

        existing = self.get_run(run_id)
        updated_summary = summary if summary is not None else existing.summary
        updated = existing.model_copy(
            update={"status": status, "updated_at": _utc_now(), "summary": updated_summary}
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?, summary_json = ?
                WHERE run_id = ?
                """,
                (
                    updated.status.value,
                    updated.updated_at.isoformat(),
                    json.dumps(updated.summary, sort_keys=True),
                    run_id,
                ),
            )
            connection.commit()
        return updated

    def get_run(self, run_id: str) -> RunRecord:
        """Fetch one run or raise if the ID is unknown."""

        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run_id: {run_id}")
        return _row_to_run(row)

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        """Return recent runs for dashboard display."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def create_approval_request(self, request: ApprovalRequest) -> None:
        """Persist a newly created approval request."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    request_id,
                    run_id,
                    workflow_id,
                    action,
                    reason,
                    status,
                    requested_by,
                    requested_at,
                    decided_at,
                    decided_by,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.run_id,
                    request.workflow_id,
                    request.action,
                    request.reason,
                    request.status.value,
                    request.requested_by,
                    request.requested_at.isoformat(),
                    request.decided_at.isoformat() if request.decided_at else None,
                    request.decided_by,
                    json.dumps(request.metadata, sort_keys=True),
                ),
            )
            connection.commit()

    def update_approval_request(self, request: ApprovalRequest) -> ApprovalRequest:
        """Persist an updated approval decision and return it."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE approvals
                SET status = ?, decided_at = ?, decided_by = ?, metadata_json = ?, reason = ?
                WHERE request_id = ?
                """,
                (
                    request.status.value,
                    request.decided_at.isoformat() if request.decided_at else None,
                    request.decided_by,
                    json.dumps(request.metadata, sort_keys=True),
                    request.reason,
                    request.request_id,
                ),
            )
            connection.commit()
        return request

    def get_approval_request(self, request_id: str) -> ApprovalRequest:
        """Fetch one approval request by ID."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown request_id: {request_id}")
        return _row_to_approval(row)

    def list_approval_requests(self, status: str | None = None) -> list[ApprovalRequest]:
        """List approval requests, optionally filtered by status."""

        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM approvals ORDER BY requested_at DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM approvals WHERE status = ? ORDER BY requested_at DESC",
                    (status,),
                ).fetchall()
        return [_row_to_approval(row) for row in rows]

    def save_reflection_report(self, report: ReflectionReport) -> None:
        """Persist one reflection report for later inspection."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO reflection_reports (
                    report_id,
                    workflow_id,
                    generated_at,
                    summary,
                    findings_json,
                    source_run_ids_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.workflow_id,
                    report.generated_at.isoformat(),
                    report.summary,
                    json.dumps([finding.model_dump(mode="json") for finding in report.findings]),
                    json.dumps(report.source_run_ids),
                ),
            )
            connection.commit()

    def list_reflection_reports(self, limit: int = 10) -> list[ReflectionReport]:
        """Return recent reflection reports for the dashboard."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reflection_reports ORDER BY generated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_reflection(row) for row in rows]

    def list_processed_item_ids(self, workflow_id: str, item_ids: list[str]) -> set[str]:
        """Return the subset of item IDs already seen for one workflow."""

        if not item_ids:
            return set()
        placeholders = ", ".join("?" for _ in item_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT item_id
                FROM workflow_items
                WHERE workflow_id = ? AND item_id IN ({placeholders})
                """,
                (workflow_id, *item_ids),
            ).fetchall()
        return {str(row["item_id"]) for row in rows}

    def list_workflow_items(
        self,
        *,
        workflow_id: str,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return stored workflow items plus decoded metadata for one workflow."""

        query = """
            SELECT workflow_id, item_id, run_id, state, first_seen_at, updated_at, metadata_json
            FROM workflow_items
            WHERE workflow_id = ?
        """
        params: tuple[Any, ...]
        if state is None:
            query += " ORDER BY updated_at DESC"
            params = (workflow_id,)
        else:
            query += " AND state = ? ORDER BY updated_at DESC"
            params = (workflow_id, state)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            metadata = json.loads(str(row["metadata_json"]))
            items.append(
                {
                    "workflow_id": str(row["workflow_id"]),
                    "item_id": str(row["item_id"]),
                    "run_id": str(row["run_id"]),
                    "state": str(row["state"]),
                    "first_seen_at": str(row["first_seen_at"]),
                    "updated_at": str(row["updated_at"]),
                    "metadata": metadata if isinstance(metadata, dict) else {},
                }
            )
        return items

    def mark_processed_items(
        self,
        *,
        workflow_id: str,
        run_id: str,
        item_ids: list[str],
        state: str = "processed",
        metadata_by_item: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Record which workflow items have already been reviewed."""

        if not item_ids:
            return
        now = _utc_now().isoformat()
        metadata_by_item = metadata_by_item or {}
        with self._connect() as connection:
            for item_id in item_ids:
                metadata_json = json.dumps(metadata_by_item.get(item_id, {}), sort_keys=True)
                connection.execute(
                    """
                    INSERT INTO workflow_items (
                        workflow_id,
                        item_id,
                        run_id,
                        state,
                        first_seen_at,
                        updated_at,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_id, item_id)
                    DO UPDATE SET
                        run_id = excluded.run_id,
                        state = excluded.state,
                        updated_at = excluded.updated_at,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        workflow_id,
                        item_id,
                        run_id,
                        state,
                        now,
                        now,
                        metadata_json,
                    ),
                )
            connection.commit()

    def upsert_task(self, record: TaskRecord) -> TaskRecord:
        """Create or update one recoverable task record."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id,
                    workflow_id,
                    source_kind,
                    source_thread_id,
                    source_message_id,
                    requested_by,
                    title,
                    task_kind,
                    status,
                    current_plan_json,
                    pending_question,
                    approval_request_ids_json,
                    linked_thread_ids_json,
                    linked_message_ids_json,
                    opaque_payload_json,
                    last_run_id,
                    failure_reason,
                    created_at,
                    updated_at,
                    completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id)
                DO UPDATE SET
                    workflow_id = excluded.workflow_id,
                    source_kind = excluded.source_kind,
                    source_thread_id = excluded.source_thread_id,
                    source_message_id = excluded.source_message_id,
                    requested_by = excluded.requested_by,
                    title = excluded.title,
                    task_kind = excluded.task_kind,
                    status = excluded.status,
                    current_plan_json = excluded.current_plan_json,
                    pending_question = excluded.pending_question,
                    approval_request_ids_json = excluded.approval_request_ids_json,
                    linked_thread_ids_json = excluded.linked_thread_ids_json,
                    linked_message_ids_json = excluded.linked_message_ids_json,
                    opaque_payload_json = excluded.opaque_payload_json,
                    last_run_id = excluded.last_run_id,
                    failure_reason = excluded.failure_reason,
                    updated_at = excluded.updated_at,
                    completed_at = excluded.completed_at
                """,
                (
                    record.task_id,
                    record.workflow_id,
                    record.source_kind,
                    record.source_thread_id,
                    record.source_message_id,
                    record.requested_by,
                    record.title,
                    record.task_kind,
                    record.status,
                    json.dumps(record.current_plan, sort_keys=True),
                    record.pending_question,
                    json.dumps(record.approval_request_ids, sort_keys=True),
                    json.dumps(record.linked_thread_ids, sort_keys=True),
                    json.dumps(record.linked_message_ids, sort_keys=True),
                    json.dumps(record.opaque_payload, sort_keys=True),
                    record.last_run_id,
                    record.failure_reason,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    record.completed_at.isoformat() if record.completed_at else None,
                ),
            )
            connection.commit()
        return record

    def get_task(self, task_id: str) -> TaskRecord:
        """Fetch one task by ID."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        return _row_to_task(row)

    def list_tasks(
        self,
        *,
        workflow_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[TaskRecord]:
        """Return recent task records, optionally filtered by workflow or status."""

        clauses: list[str] = []
        params: list[Any] = []
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM tasks
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def list_tasks_for_thread(
        self,
        *,
        workflow_id: str,
        source_thread_id: str,
        statuses: list[str] | None = None,
        limit: int = 20,
    ) -> list[TaskRecord]:
        """Return recent task records originating from one thread."""

        with self._connect() as connection:
            if statuses:
                placeholders = ", ".join("?" for _ in statuses)
                rows = connection.execute(
                    f"""
                    SELECT * FROM tasks
                    WHERE workflow_id = ? AND source_thread_id = ? AND status IN ({placeholders})
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (workflow_id, source_thread_id, *statuses, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM tasks
                    WHERE workflow_id = ? AND source_thread_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (workflow_id, source_thread_id, limit),
                ).fetchall()
        return [_row_to_task(row) for row in rows]

    def upsert_task_steps(self, records: list[TaskStepRecord]) -> None:
        """Append step history and refresh the latest known step state."""

        if not records:
            return
        with self._connect() as connection:
            for index, record in enumerate(records):
                event_id = (
                    f"{record.task_id}:{record.step_id}:{record.run_id}:"
                    f"{record.updated_at.isoformat()}:{index}"
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO task_step_events (
                        event_id,
                        task_id,
                        step_id,
                        workflow_id,
                        run_id,
                        step_kind,
                        description,
                        status,
                        approval_required,
                        sequence_number,
                        payload_json,
                        error,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        record.task_id,
                        record.step_id,
                        record.workflow_id,
                        record.run_id,
                        record.step_kind,
                        record.description,
                        record.status,
                        1 if record.approval_required else 0,
                        record.sequence_number,
                        json.dumps(record.payload, sort_keys=True),
                        record.error,
                        record.updated_at.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO task_steps (
                        task_id,
                        step_id,
                        workflow_id,
                        run_id,
                        step_kind,
                        description,
                        status,
                        approval_required,
                        sequence_number,
                        payload_json,
                        error,
                        created_at,
                        updated_at,
                        completed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, step_id)
                    DO UPDATE SET
                        workflow_id = excluded.workflow_id,
                        run_id = excluded.run_id,
                        step_kind = excluded.step_kind,
                        description = excluded.description,
                        status = excluded.status,
                        approval_required = excluded.approval_required,
                        sequence_number = excluded.sequence_number,
                        payload_json = excluded.payload_json,
                        error = excluded.error,
                        updated_at = excluded.updated_at,
                        completed_at = excluded.completed_at
                    """,
                    (
                        record.task_id,
                        record.step_id,
                        record.workflow_id,
                        record.run_id,
                        record.step_kind,
                        record.description,
                        record.status,
                        1 if record.approval_required else 0,
                        record.sequence_number,
                        json.dumps(record.payload, sort_keys=True),
                        record.error,
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                        record.completed_at.isoformat() if record.completed_at else None,
                    ),
                )
            connection.commit()

    def list_task_steps(self, *, task_id: str) -> list[TaskStepRecord]:
        """Return the materialized latest ordered step state for one task."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_steps
                WHERE task_id = ?
                ORDER BY
                    CASE WHEN sequence_number IS NULL THEN 1 ELSE 0 END,
                    sequence_number ASC,
                    updated_at ASC
                """,
                (task_id,),
            ).fetchall()
        return [_row_to_task_step(row) for row in rows]

    def list_task_step_events(
        self,
        *,
        task_id: str,
        step_id: str | None = None,
    ) -> list[TaskStepRecord]:
        """Return append-only step history for one task."""

        query = """
            SELECT *
            FROM task_step_events
            WHERE task_id = ?
        """
        params: tuple[Any, ...]
        if step_id is None:
            query += " ORDER BY created_at ASC"
            params = (task_id,)
        else:
            query += " AND step_id = ? ORDER BY created_at ASC"
            params = (task_id, step_id)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_row_to_task_step_event(row) for row in rows]

    def append_task_events(self, records: list[TaskEventRecord]) -> None:
        """Persist append-only task events."""

        if not records:
            return
        with self._connect() as connection:
            for record in records:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO task_events (
                        event_id,
                        task_id,
                        workflow_id,
                        run_id,
                        event_kind,
                        summary,
                        step_id,
                        step_kind,
                        status,
                        sequence_number,
                        payload_json,
                        error,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.event_id,
                        record.task_id,
                        record.workflow_id,
                        record.run_id,
                        record.event_kind,
                        record.summary,
                        record.step_id,
                        record.step_kind,
                        record.status,
                        record.sequence_number,
                        json.dumps(record.payload, sort_keys=True),
                        record.error,
                        record.created_at.isoformat(),
                    ),
                )
            connection.commit()

    def list_task_events(self, *, task_id: str, limit: int | None = None) -> list[TaskEventRecord]:
        """Return append-only task events for one task."""

        query = """
            SELECT * FROM task_events
            WHERE task_id = ?
            ORDER BY created_at ASC
        """
        params: tuple[Any, ...]
        if limit is None:
            params = (task_id,)
        else:
            query += " LIMIT ?"
            params = (task_id, limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_row_to_task_event(row) for row in rows]

    def create_slack_question(self, record: SlackQuestionRecord) -> None:
        """Persist one Slack question posted by SAI."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO slack_questions (
                    question_id,
                    workflow_id,
                    run_id,
                    item_id,
                    channel_id,
                    thread_ts,
                    question_text,
                    status,
                    created_at,
                    updated_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.question_id,
                    record.workflow_id,
                    record.run_id,
                    record.item_id,
                    record.channel_id,
                    record.thread_ts,
                    record.question_text,
                    record.status,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    json.dumps(record.metadata, sort_keys=True),
                ),
            )
            connection.commit()

    def update_slack_question(self, record: SlackQuestionRecord) -> SlackQuestionRecord:
        """Persist question status or metadata updates."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE slack_questions
                SET status = ?, updated_at = ?, metadata_json = ?
                WHERE question_id = ?
                """,
                (
                    record.status,
                    record.updated_at.isoformat(),
                    json.dumps(record.metadata, sort_keys=True),
                    record.question_id,
                ),
            )
            connection.commit()
        return record

    def get_slack_question(self, question_id: str) -> SlackQuestionRecord:
        """Fetch one Slack question by its ID."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM slack_questions WHERE question_id = ?",
                (question_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown slack question: {question_id}")
        return _row_to_slack_question(row)

    def get_slack_question_by_thread(
        self,
        *,
        channel_id: str,
        thread_ts: str,
    ) -> SlackQuestionRecord | None:
        """Return the question attached to one Slack thread if known."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM slack_questions
                WHERE channel_id = ? AND thread_ts = ?
                """,
                (channel_id, thread_ts),
            ).fetchone()
        if row is None:
            return None
        return _row_to_slack_question(row)

    def list_slack_questions(self, limit: int = 20) -> list[SlackQuestionRecord]:
        """Return recent Slack questions for inspection."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM slack_questions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_slack_question(row) for row in rows]

    def list_slack_questions_for_item(
        self,
        *,
        workflow_id: str,
        item_id: str,
        statuses: list[SlackQuestionStatus] | None = None,
        limit: int = 20,
    ) -> list[SlackQuestionRecord]:
        """Return recent Slack questions for one workflow item."""

        with self._connect() as connection:
            if statuses:
                placeholders = ", ".join("?" for _ in statuses)
                rows = connection.execute(
                    f"""
                    SELECT * FROM slack_questions
                    WHERE workflow_id = ? AND item_id = ? AND status IN ({placeholders})
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (workflow_id, item_id, *statuses, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM slack_questions
                    WHERE workflow_id = ? AND item_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (workflow_id, item_id, limit),
                ).fetchall()
        return [_row_to_slack_question(row) for row in rows]

    def create_slack_feedback(self, record: SlackFeedbackRecord) -> None:
        """Persist one inbound Slack feedback message or button action."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO slack_feedback (
                    feedback_id,
                    question_id,
                    workflow_id,
                    run_id,
                    item_id,
                    slack_user_id,
                    channel_id,
                    thread_ts,
                    message_ts,
                    feedback_type,
                    text,
                    action_id,
                    value,
                    created_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.feedback_id,
                    record.question_id,
                    record.workflow_id,
                    record.run_id,
                    record.item_id,
                    record.slack_user_id,
                    record.channel_id,
                    record.thread_ts,
                    record.message_ts,
                    record.feedback_type,
                    record.text,
                    record.action_id,
                    record.value,
                    record.created_at.isoformat(),
                    json.dumps(record.metadata, sort_keys=True),
                ),
            )
            connection.commit()

    def list_slack_feedback(
        self,
        *,
        question_id: str | None = None,
        limit: int = 50,
    ) -> list[SlackFeedbackRecord]:
        """Return recent Slack feedback, optionally scoped to one question."""

        with self._connect() as connection:
            if question_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM slack_feedback
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM slack_feedback
                    WHERE question_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (question_id, limit),
                ).fetchall()
        return [_row_to_slack_feedback(row) for row in rows]

    def has_slack_feedback_message(self, *, channel_id: str, message_ts: str) -> bool:
        """Return whether one Slack message has already been recorded."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM slack_feedback
                WHERE channel_id = ? AND message_ts = ?
                LIMIT 1
                """,
                (channel_id, message_ts),
            ).fetchone()
        return row is not None


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    """Translate a SQLite row back into the shared run schema."""

    from datetime import datetime

    return RunRecord(
        run_id=row["run_id"],
        workflow_id=row["workflow_id"],
        status=RunStatus(row["status"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        summary=json.loads(row["summary_json"]),
    )


def _row_to_task(row: sqlite3.Row) -> TaskRecord:
    """Translate a SQLite row into a recoverable task record."""

    from datetime import datetime

    completed_at = row["completed_at"]
    return TaskRecord(
        task_id=str(row["task_id"]),
        workflow_id=str(row["workflow_id"]),
        source_kind=str(row["source_kind"]),
        source_thread_id=(
            str(row["source_thread_id"]) if row["source_thread_id"] is not None else None
        ),
        source_message_id=(
            str(row["source_message_id"]) if row["source_message_id"] is not None else None
        ),
        requested_by=str(row["requested_by"]) if row["requested_by"] is not None else None,
        title=str(row["title"]),
        task_kind=str(row["task_kind"]) if row["task_kind"] is not None else None,
        status=cast("Any", str(row["status"])),
        current_plan=json.loads(str(row["current_plan_json"])),
        pending_question=(
            str(row["pending_question"]) if row["pending_question"] is not None else None
        ),
        approval_request_ids=json.loads(str(row["approval_request_ids_json"])),
        linked_thread_ids=json.loads(str(row["linked_thread_ids_json"])),
        linked_message_ids=json.loads(str(row["linked_message_ids_json"])),
        opaque_payload=json.loads(str(row["opaque_payload_json"])),
        last_run_id=str(row["last_run_id"]) if row["last_run_id"] is not None else None,
        failure_reason=(str(row["failure_reason"]) if row["failure_reason"] is not None else None),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        completed_at=datetime.fromisoformat(str(completed_at)) if completed_at else None,
    )


def _row_to_task_step(row: sqlite3.Row) -> TaskStepRecord:
    """Translate a SQLite row into one task-step record."""

    from datetime import datetime

    completed_at = row["completed_at"]
    return TaskStepRecord(
        task_id=str(row["task_id"]),
        step_id=str(row["step_id"]),
        workflow_id=str(row["workflow_id"]),
        run_id=str(row["run_id"]),
        step_kind=str(row["step_kind"]),
        description=str(row["description"]),
        status=cast("Any", str(row["status"])),
        approval_required=bool(row["approval_required"]),
        sequence_number=int(row["sequence_number"]) if row["sequence_number"] is not None else None,
        payload=json.loads(str(row["payload_json"])),
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        completed_at=datetime.fromisoformat(str(completed_at)) if completed_at else None,
    )


def _row_to_task_step_event(row: sqlite3.Row) -> TaskStepRecord:
    """Translate a SQLite row into one append-only task-step event."""

    from datetime import datetime

    return TaskStepRecord(
        task_id=str(row["task_id"]),
        step_id=str(row["step_id"]),
        workflow_id=str(row["workflow_id"]),
        run_id=str(row["run_id"]),
        step_kind=str(row["step_kind"]),
        description=str(row["description"]),
        status=cast("Any", str(row["status"])),
        approval_required=bool(row["approval_required"]),
        sequence_number=int(row["sequence_number"]) if row["sequence_number"] is not None else None,
        payload=json.loads(str(row["payload_json"])),
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["created_at"])),
        completed_at=(
            datetime.fromisoformat(str(row["created_at"]))
            if str(row["status"]) == "completed"
            else None
        ),
    )


def _row_to_task_event(row: sqlite3.Row) -> TaskEventRecord:
    """Translate a SQLite row into one append-only task event."""

    from datetime import datetime

    return TaskEventRecord(
        event_id=str(row["event_id"]),
        task_id=str(row["task_id"]),
        workflow_id=str(row["workflow_id"]),
        run_id=str(row["run_id"]),
        event_kind=cast("Any", str(row["event_kind"])),
        summary=str(row["summary"]),
        step_id=str(row["step_id"]) if row["step_id"] is not None else None,
        step_kind=str(row["step_kind"]) if row["step_kind"] is not None else None,
        status=str(row["status"]) if row["status"] is not None else None,
        sequence_number=int(row["sequence_number"]) if row["sequence_number"] is not None else None,
        payload=json.loads(str(row["payload_json"])),
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


def _row_to_slack_question(row: sqlite3.Row) -> SlackQuestionRecord:
    """Translate a SQLite row into the Slack question schema."""

    from datetime import datetime

    return SlackQuestionRecord(
        question_id=str(row["question_id"]),
        workflow_id=str(row["workflow_id"]),
        run_id=str(row["run_id"]) if row["run_id"] is not None else None,
        item_id=str(row["item_id"]),
        channel_id=str(row["channel_id"]),
        thread_ts=str(row["thread_ts"]),
        question_text=str(row["question_text"]),
        status=cast(
            "SlackQuestionStatus",
            str(row["status"]),
        ),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        metadata=json.loads(str(row["metadata_json"])),
    )


def _row_to_slack_feedback(row: sqlite3.Row) -> SlackFeedbackRecord:
    """Translate a SQLite row into the Slack feedback schema."""

    from datetime import datetime

    return SlackFeedbackRecord(
        feedback_id=str(row["feedback_id"]),
        question_id=str(row["question_id"]) if row["question_id"] is not None else None,
        workflow_id=str(row["workflow_id"]) if row["workflow_id"] is not None else None,
        run_id=str(row["run_id"]) if row["run_id"] is not None else None,
        item_id=str(row["item_id"]) if row["item_id"] is not None else None,
        slack_user_id=str(row["slack_user_id"]),
        channel_id=str(row["channel_id"]),
        thread_ts=str(row["thread_ts"]),
        message_ts=str(row["message_ts"]),
        feedback_type=cast(
            "SlackFeedbackType",
            str(row["feedback_type"]),
        ),
        text=str(row["text"]) if row["text"] is not None else None,
        action_id=str(row["action_id"]) if row["action_id"] is not None else None,
        value=str(row["value"]) if row["value"] is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        metadata=json.loads(str(row["metadata_json"])),
    )


def _row_to_approval(row: sqlite3.Row) -> ApprovalRequest:
    """Translate a SQLite row back into the shared approval schema."""

    from datetime import datetime

    from app.shared.models import ApprovalStatus

    decided_at = row["decided_at"]
    return ApprovalRequest(
        request_id=row["request_id"],
        run_id=row["run_id"],
        workflow_id=row["workflow_id"],
        action=row["action"],
        reason=row["reason"],
        status=ApprovalStatus(row["status"]),
        requested_by=row["requested_by"],
        requested_at=datetime.fromisoformat(row["requested_at"]),
        decided_at=datetime.fromisoformat(decided_at) if decided_at else None,
        decided_by=row["decided_by"],
        metadata=json.loads(row["metadata_json"]),
    )


def _row_to_reflection(row: sqlite3.Row) -> ReflectionReport:
    """Translate a SQLite row back into the reflection report schema."""

    from datetime import datetime

    from app.reflection.models import ReflectionFinding

    return ReflectionReport(
        report_id=row["report_id"],
        workflow_id=row["workflow_id"],
        generated_at=datetime.fromisoformat(row["generated_at"]),
        summary=row["summary"],
        findings=[
            ReflectionFinding.model_validate(item) for item in json.loads(row["findings_json"])
        ],
        source_run_ids=json.loads(row["source_run_ids_json"]),
    )


def _utc_now() -> datetime:
    """Use one UTC timestamp helper for store updates."""

    return datetime.now(UTC)
