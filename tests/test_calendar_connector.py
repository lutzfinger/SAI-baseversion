from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.connectors import calendar_auth
from app.connectors.calendar_auth import (
    CalendarAuthConfigurationError,
    CalendarOAuthAuthenticator,
)
from app.control_plane.loaders import PolicyStore
from app.shared.config import Settings


class _FakeCredentials:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.valid = True
        self.expired = False
        self.refresh_token = payload.get("refresh_token")
        self.granted_scopes = payload.get("granted_scopes")

    def to_json(self) -> str:
        return json.dumps(self.payload)


class _FakeFlow:
    def __init__(self) -> None:
        self.open_browser: bool | None = None

    def run_local_server(self, **kwargs: Any) -> _FakeCredentials:
        self.open_browser = bool(kwargs["open_browser"])
        return _FakeCredentials({"token": "calendar-token", "refresh_token": "refresh-me"})


def test_calendar_authenticator_runs_browser_flow_and_writes_token(
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = PolicyStore(test_settings.policies_dir).load("meeting_decision.yaml")
    credentials_path = tmp_path / "client_secret.json"
    credentials_path.write_text(json.dumps({"installed": {"client_id": "abc"}}), encoding="utf-8")
    token_path = tmp_path / "meeting_calendar_token.json"
    fake_flow = _FakeFlow()

    def _fake_build_flow(path: Path, scopes: list[str]) -> _FakeFlow:
        assert path == credentials_path
        assert "https://www.googleapis.com/auth/calendar.readonly" in scopes
        return fake_flow

    monkeypatch.setenv("SAI_GMAIL_CREDENTIALS_PATH", str(credentials_path))
    monkeypatch.setenv("SAI_MEETING_CALENDAR_TOKEN_PATH", str(token_path))
    monkeypatch.setattr(
        calendar_auth,
        "_build_installed_app_flow_from_file",
        _fake_build_flow,
    )

    authenticator = CalendarOAuthAuthenticator(settings=test_settings, policy=policy)
    returned_token_path = authenticator.authenticate_interactively(open_browser=False)

    assert returned_token_path == token_path
    assert fake_flow.open_browser is False
    saved_payload = json.loads(token_path.read_text(encoding="utf-8"))
    assert saved_payload["token"] == "calendar-token"


def test_calendar_authenticator_fails_closed_for_noninteractive_runs(
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = PolicyStore(test_settings.policies_dir).load("meeting_decision.yaml")
    credentials_path = tmp_path / "client_secret.json"
    credentials_path.write_text(json.dumps({"installed": {"client_id": "abc"}}), encoding="utf-8")
    token_path = tmp_path / "meeting_calendar_token.json"

    monkeypatch.setenv("SAI_GMAIL_CREDENTIALS_PATH", str(credentials_path))
    monkeypatch.setenv("SAI_MEETING_CALENDAR_TOKEN_PATH", str(token_path))

    def _unexpected_interactive_flow(path: Path, scopes: list[str]) -> _FakeFlow:
        del path, scopes
        raise AssertionError("interactive flow should not run")

    monkeypatch.setattr(
        calendar_auth,
        "_build_installed_app_flow_from_file",
        _unexpected_interactive_flow,
    )

    authenticator = CalendarOAuthAuthenticator(settings=test_settings, policy=policy)

    with pytest.raises(
        CalendarAuthConfigurationError,
        match="Interactive Calendar OAuth is disabled",
    ):
        authenticator.build_service()


def test_calendar_authenticator_reauths_when_token_scopes_are_too_narrow(
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = PolicyStore(test_settings.policies_dir).load("travel_operation_execution.yaml")
    credentials_path = tmp_path / "client_secret.json"
    credentials_path.write_text(json.dumps({"installed": {"client_id": "abc"}}), encoding="utf-8")
    token_path = tmp_path / "meeting_calendar_token.json"
    token_path.write_text(
        json.dumps({"token": "calendar-token", "refresh_token": "refresh-me"}),
        encoding="utf-8",
    )
    fake_flow = _FakeFlow()

    monkeypatch.setenv("SAI_GMAIL_CREDENTIALS_PATH", str(credentials_path))
    monkeypatch.setenv("SAI_MEETING_CALENDAR_TOKEN_PATH", str(token_path))
    monkeypatch.setattr(
        calendar_auth,
        "_load_credentials_from_token_file",
        lambda path, scopes: _FakeCredentials(
            {
                "token": "calendar-token",
                "refresh_token": "refresh-me",
                "granted_scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
            }
        ),
    )
    monkeypatch.setattr(
        calendar_auth,
        "_build_installed_app_flow_from_file",
        lambda path, scopes: fake_flow,
    )

    authenticator = CalendarOAuthAuthenticator(settings=test_settings, policy=policy)
    returned_token_path = authenticator.authenticate_interactively(open_browser=False)

    assert returned_token_path == token_path
    assert fake_flow.open_browser is False
