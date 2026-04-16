from __future__ import annotations

from pathlib import Path

import pytest

from app.shared.config import Settings


@pytest.fixture
def starter_settings(tmp_path: Path) -> Settings:
    logs_dir = tmp_path / "logs"
    learning_dir = logs_dir / "learning"
    return Settings(
        logs_dir=logs_dir,
        artifacts_dir=logs_dir / "artifacts",
        learning_dir=learning_dir,
        audit_log_path=logs_dir / "audit.jsonl",
        database_path=logs_dir / "control_plane.db",
        fact_memory_database_path=learning_dir / "fact_memory.sqlite",
        newsletter_eval_dataset_path=learning_dir / "newsletter_eval_dataset.jsonl",
        sai_email_activity_log_path=learning_dir / "sai_email_activities.jsonl",
        sai_email_golden_dataset_path=learning_dir / "sai_email_golden_dataset.jsonl",
        openai_api_key="test-key",
    )
