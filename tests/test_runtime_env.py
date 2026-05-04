from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.shared.runtime_env import load_runtime_env_best_effort


def test_load_runtime_env_best_effort_sets_plain_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "runtime.env"
    env_path.write_text(
        'SAI_GMAIL_CREDENTIALS_PATH="/tmp/google_client_secret.json"\n'
        'SAI_LANGSMITH_ENABLED="true"\n',
        encoding="utf-8",
    )

    monkeypatch.delenv("SAI_GMAIL_CREDENTIALS_PATH", raising=False)
    monkeypatch.delenv("SAI_LANGSMITH_ENABLED", raising=False)

    load_runtime_env_best_effort(runtime_env_path=env_path)

    assert os.getenv("SAI_GMAIL_CREDENTIALS_PATH") == "/tmp/google_client_secret.json"
    assert os.getenv("SAI_LANGSMITH_ENABLED") == "true"


def test_load_runtime_env_best_effort_does_not_override_existing_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "runtime.env"
    env_path.write_text(
        'SAI_GMAIL_CREDENTIALS_PATH="/tmp/google_client_secret.json"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("SAI_GMAIL_CREDENTIALS_PATH", "/already/set.json")

    load_runtime_env_best_effort(runtime_env_path=env_path)

    assert os.getenv("SAI_GMAIL_CREDENTIALS_PATH") == "/already/set.json"


def test_load_runtime_env_best_effort_expands_home_and_env_vars(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "runtime.env"
    monkeypatch.setenv("HOME", "/tmp/test-home")
    monkeypatch.setenv("SAI_FAKE_ROOT", "/tmp/fake-root")
    monkeypatch.delenv("SAI_GMAIL_CREDENTIALS_PATH", raising=False)
    monkeypatch.delenv("SAI_GMAIL_TOKEN_PATH", raising=False)
    env_path.write_text(
        'SAI_GMAIL_CREDENTIALS_PATH="$HOME/credentials/google_client_secret.json"\n'
        'SAI_GMAIL_TOKEN_PATH="$SAI_FAKE_ROOT/tokens/gmail.json"\n',
        encoding="utf-8",
    )

    load_runtime_env_best_effort(runtime_env_path=env_path)

    assert (
        os.getenv("SAI_GMAIL_CREDENTIALS_PATH")
        == "/tmp/test-home/credentials/google_client_secret.json"
    )
    assert os.getenv("SAI_GMAIL_TOKEN_PATH") == "/tmp/fake-root/tokens/gmail.json"
