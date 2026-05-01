"""Shared runtime configuration for the SAI starter repo."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.shared.runtime_env import load_runtime_env_best_effort

REPO_ROOT = Path(__file__).resolve().parents[2]

# Runtime state lives outside the repo so the working tree stays code/config only.
# Override via SAI_STATE_DIR / SAI_LOGS_DIR / SAI_TOKENS_DIR env vars (pydantic
# Settings reads SAI_<FIELD> automatically via the env_prefix below).
#
#   DEFAULT_STATE_DIR        — mutable runtime state (sqlite DBs, checkpoints,
#                              background-service heartbeats). macOS standard:
#                              ~/Library/Application Support/SAI/state
#   DEFAULT_LOG_DIR          — append-only logs (audit.jsonl, scheduled-job
#                              stdout/err, ollama-auto.log). macOS standard:
#                              ~/Library/Logs/SAI
#   DEFAULT_TOKENS_DIR       — OAuth tokens and secrets staged for connectors.
#                              ~/.config/sai/tokens (kept separate from state).
#   DEFAULT_EVAL_DIR         — VERSIONED eval datasets, training artifacts that
#                              SHOULD be checked in (or kept under version
#                              control in the private overlay). REPO_ROOT/eval.
#                              Read-only at runtime; rebuilt by --build.
#   DEFAULT_EVAL_RUNTIME_DIR — STATEFUL eval runtime: open Asks, EvalRecord
#                              JSONL, reconciliation queues. Lives under the OS
#                              state dir so `sai_cutover.sh --build --clean`
#                              doesn't nuke production records (principle #8).
#                              ~/Library/Application Support/SAI/state/eval.
DEFAULT_STATE_DIR = Path("~/Library/Application Support/SAI/state").expanduser()
DEFAULT_LOG_DIR = Path("~/Library/Logs/SAI").expanduser()
DEFAULT_TOKENS_DIR = Path("~/.config/sai/tokens").expanduser()
DEFAULT_EVAL_DIR = REPO_ROOT / "eval"
DEFAULT_EVAL_RUNTIME_DIR = DEFAULT_STATE_DIR / "eval"


class Settings(BaseSettings):
    """Small, explicit runtime settings for the starter control plane."""

    app_name: str = "SAI Baseversion"
    environment: str = "local"
    operator_id: str = "local-operator"
    user_email: str = "you@example.com"
    sai_alias_email: str = Field(
        default="sai@example.com",
        validation_alias=AliasChoices("SAI_ALIAS_EMAIL", "SAI_SAI_ALIAS_EMAIL"),
    )
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
    langsmith_tracing: bool = Field(
        default=False,
        validation_alias=AliasChoices("SAI_LANGSMITH_ENABLED", "LANGSMITH_TRACING"),
    )
    langsmith_project: str = Field(
        default="sai-baseversion",
        validation_alias=AliasChoices(
            "SAI_LANGSMITH_PROJECT",
            "LANGSMITH_PROJECT",
            "LANGCHAIN_PROJECT",
        ),
    )
    langsmith_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SAI_LANGSMITH_ENDPOINT", "LANGSMITH_ENDPOINT"),
    )
    langsmith_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SAI_LANGSMITH_API_KEY", "LANGSMITH_API_KEY"),
    )
    langsmith_workspace_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SAI_LANGSMITH_WORKSPACE_ID",
            "LANGSMITH_WORKSPACE_ID",
        ),
    )

    root_dir: Path = Field(default=REPO_ROOT)
    prompts_dir: Path = Field(default=REPO_ROOT / "prompts")
    policies_dir: Path = Field(default=REPO_ROOT / "policies")
    workflows_dir: Path = Field(default=REPO_ROOT / "workflows")
    registry_dir: Path = Field(default=REPO_ROOT / "registry")
    config_dir: Path = Field(default=REPO_ROOT / "config")
    state_dir: Path = Field(default=DEFAULT_STATE_DIR)
    logs_dir: Path = Field(default=DEFAULT_LOG_DIR)
    tokens_dir: Path = Field(default=DEFAULT_TOKENS_DIR)
    artifacts_dir: Path = Field(default=DEFAULT_STATE_DIR / "artifacts")
    # learning_dir = VERSIONED datasets in the repo (training fixtures,
    # golden datasets). Kept under version control. `--build --clean`
    # rebuilds this from the repos so it's expected to be transient on
    # disk but reproducible from source.
    learning_dir: Path = Field(default=DEFAULT_EVAL_DIR)
    # eval_runtime_dir = STATEFUL eval runtime (open Asks, EvalRecord
    # JSONL, reconciliation pointers). Lives under the OS state dir so
    # `--build --clean` doesn't wipe production records. Per principle #8.
    eval_runtime_dir: Path = Field(default=DEFAULT_EVAL_RUNTIME_DIR)
    audit_log_path: Path = Field(default=DEFAULT_LOG_DIR / "audit.jsonl")
    database_path: Path = Field(default=DEFAULT_STATE_DIR / "control_plane.db")
    fact_memory_database_path: Path = Field(
        default=DEFAULT_STATE_DIR / "fact_memory.sqlite"
    )
    newsletter_eval_dataset_path: Path = Field(
        default=DEFAULT_EVAL_DIR / "newsletter_eval_dataset.jsonl"
    )
    sai_email_activity_log_path: Path = Field(
        default=DEFAULT_EVAL_DIR / "sai_email_activities.jsonl"
    )
    sai_email_golden_dataset_path: Path = Field(
        default=DEFAULT_EVAL_DIR / "sai_email_golden_dataset.jsonl"
    )

    templates_dir: Path = Field(default=REPO_ROOT / "app" / "ui" / "templates")

    # Phase 1 (overlay hash verification): when set, points at the merged
    # runtime tree produced by `sai-overlay merge`. The hash-verifying loader
    # uses this to find `.sai-overlay-manifest.json` and verify every workflow
    # / policy / prompt before parsing. Unset means no overlay merge is in
    # effect (e.g. running directly out of the repo for the public starter)
    # and the loader skips verification.
    overlay_runtime_root: Path | None = Field(default=None)

    model_config = SettingsConfigDict(
        env_prefix="SAI_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    def ensure_runtime_paths(self) -> None:
        """Create the local directories the starter control plane expects."""

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.learning_dir.mkdir(parents=True, exist_ok=True)
        self.eval_runtime_dir.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        self.policies_dir.mkdir(parents=True, exist_ok=True)
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def langsmith_tracing_enabled(self) -> bool:
        """Return whether LangSmith tracing is fully configured and enabled."""

        return bool(self.langsmith_tracing and self.langsmith_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one shared process-level settings object."""

    load_runtime_env_best_effort()
    settings = Settings()
    settings.ensure_runtime_paths()
    return settings
