"""Shared runtime configuration for the SAI starter repo."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.shared.runtime_env import load_runtime_env_best_effort

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Small, explicit runtime settings for the starter control plane."""

    app_name: str = "SAI Baseversion"
    environment: str = "local"
    operator_id: str = "local-operator"
    user_email: str = "you@example.com"
    sai_alias_email: str = "sai@example.com"
    log_level: str = "INFO"
    max_logged_snippet_chars: int = 160
    max_email_body_chars: int = 4000

    local_llm_enabled: bool = True
    local_llm_host: str = "http://127.0.0.1:11434"
    local_llm_model: str = "gpt-oss:20b"
    local_llm_timeout_seconds: int = 45
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SAI_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openai_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SAI_OPENAI_BASE_URL", "OPENAI_BASE_URL"),
    )
    openai_timeout_seconds: int = 45

    graph_checkpoint_path: Path = Field(default=REPO_ROOT / "logs" / "langgraph_checkpoints.sqlite")
    root_dir: Path = Field(default=REPO_ROOT)
    prompts_dir: Path = Field(default=REPO_ROOT / "prompts")
    policies_dir: Path = Field(default=REPO_ROOT / "policies")
    workflows_dir: Path = Field(default=REPO_ROOT / "workflows")
    registry_dir: Path = Field(default=REPO_ROOT / "registry")
    config_dir: Path = Field(default=REPO_ROOT / "config")
    logs_dir: Path = Field(default=REPO_ROOT / "logs")
    artifacts_dir: Path = Field(default=REPO_ROOT / "logs" / "artifacts")
    learning_dir: Path = Field(default=REPO_ROOT / "logs" / "learning")
    audit_log_path: Path = Field(default=REPO_ROOT / "logs" / "audit.jsonl")
    database_path: Path = Field(default=REPO_ROOT / "logs" / "control_plane.db")
    fact_memory_database_path: Path = Field(
        default=REPO_ROOT / "logs" / "learning" / "fact_memory.sqlite"
    )
    newsletter_eval_dataset_path: Path = Field(
        default=REPO_ROOT / "logs" / "learning" / "newsletter_eval_dataset.jsonl"
    )
    sai_email_activity_log_path: Path = Field(
        default=REPO_ROOT / "logs" / "learning" / "sai_email_activities.jsonl"
    )
    sai_email_golden_dataset_path: Path = Field(
        default=REPO_ROOT / "logs" / "learning" / "sai_email_golden_dataset.jsonl"
    )

    templates_dir: Path = Field(default=REPO_ROOT / "app" / "ui" / "templates")

    model_config = SettingsConfigDict(env_prefix="SAI_", env_file=".env", extra="ignore")

    def ensure_runtime_paths(self) -> None:
        """Create the local directories the starter control plane expects."""

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.learning_dir.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        self.policies_dir.mkdir(parents=True, exist_ok=True)
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one shared process-level settings object."""

    load_runtime_env_best_effort()
    settings = Settings()
    settings.ensure_runtime_paths()
    return settings
