from __future__ import annotations

from pathlib import Path

import pytest

from app.shared.config import Settings

# Tests left from the Phase 3 split that need either a richer test_settings
# fixture or env-isolation work before they can run. Each comes back in its
# corresponding task migration (see MIGRATION-BACKLOG.md). Tests whose deps
# went away with the dropped modules have been removed entirely; what's left
# below is the residue waiting on framework wiring in their own tasks.
collect_ignore_glob = [
    # Need richer test_settings fixture (private's conftest pattern, sanitized)
    "test_approvals.py",
    "test_background_services.py",
    "test_calendar_connector.py",
    "test_fact_memory.py",
    "test_prompt_hashes.py",
    "test_reflection.py",
    "test_replay.py",
    # Pollutes os.environ via load_runtime_env_best_effort(); breaks
    # test_langsmith_settings until test isolation is added
    "test_runtime_env.py",
    # Returns empty taxonomy without private's taxonomy data
    "test_gmail_taxonomy_labels.py",
]


@pytest.fixture
def starter_settings(tmp_path: Path) -> Settings:
    # Pin every path field that defaults to ~/Library/... or REPO_ROOT/eval to a
    # tmp_path subdir, so tests never read or write the real user's runtime
    # state, logs, tokens, or eval datasets.
    state_dir = tmp_path / "state"
    logs_dir = tmp_path / "logs"
    tokens_dir = tmp_path / "tokens"
    learning_dir = tmp_path / "eval"
    eval_runtime_dir = tmp_path / "eval_runtime"
    return Settings(
        state_dir=state_dir,
        logs_dir=logs_dir,
        tokens_dir=tokens_dir,
        artifacts_dir=state_dir / "artifacts",
        learning_dir=learning_dir,
        eval_runtime_dir=eval_runtime_dir,
        audit_log_path=logs_dir / "audit.jsonl",
        database_path=state_dir / "control_plane.db",
        fact_memory_database_path=state_dir / "fact_memory.sqlite",
        newsletter_eval_dataset_path=learning_dir / "newsletter_eval_dataset.jsonl",
        sai_email_activity_log_path=learning_dir / "sai_email_activities.jsonl",
        sai_email_golden_dataset_path=learning_dir / "sai_email_golden_dataset.jsonl",
        openai_api_key="test-key",
        langsmith_tracing=False,
        langsmith_project="test-suite",
        langsmith_api_key=None,
        langsmith_endpoint=None,
        langsmith_workspace_id=None,
    )
