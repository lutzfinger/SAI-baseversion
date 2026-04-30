from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from app.shared.config import Settings


def test_settings_accept_standard_langsmith_env_names(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    # AliasChoices prefers the SAI_-prefixed names. The user's runtime.env
    # (loaded by load_runtime_env_best_effort) may have set SAI_LANGSMITH_*,
    # which would shadow the unprefixed env vars this test is exercising.
    # Unset the SAI_-prefixed ones so the unprefixed names actually take effect.
    for sai_var in (
        "SAI_LANGSMITH_TRACING",
        "SAI_LANGSMITH_PROJECT",
        "SAI_LANGSMITH_API_KEY",
        "SAI_LANGSMITH_ENDPOINT",
        "SAI_LANGSMITH_WORKSPACE_ID",
    ):
        monkeypatch.delenv(sai_var, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_PROJECT", "starter-traces")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    monkeypatch.setenv("LANGSMITH_WORKSPACE_ID", "workspace-123")

    settings = Settings()

    assert settings.langsmith_tracing is True
    assert settings.langsmith_project == "starter-traces"
    assert settings.langsmith_api_key == "test-key"
    assert settings.langsmith_endpoint == "https://api.smith.langchain.com"
    assert settings.langsmith_workspace_id == "workspace-123"
    assert settings.langsmith_tracing_enabled() is True


def test_settings_accept_direct_field_names() -> None:
    settings = Settings(
        openai_api_key="openai-test-key",
        langsmith_tracing=True,
        langsmith_project="starter-direct",
        langsmith_api_key="langsmith-test-key",
        langsmith_endpoint="https://api.smith.langchain.com",
        langsmith_workspace_id="workspace-456",
    )

    assert settings.openai_api_key == "openai-test-key"
    assert settings.langsmith_project == "starter-direct"
    assert settings.langsmith_api_key == "langsmith-test-key"
    assert settings.langsmith_endpoint == "https://api.smith.langchain.com"
    assert settings.langsmith_workspace_id == "workspace-456"
    assert settings.langsmith_tracing_enabled() is True
