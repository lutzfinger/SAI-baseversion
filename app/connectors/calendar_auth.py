"""Official Google Calendar OAuth helper for local desktop testing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.connectors.calendar_config import CalendarConnectorPolicy
from app.connectors.gmail_auth import (
    GOOGLE_AUTH_URI,
    GOOGLE_TOKEN_URI,
    _build_installed_app_flow_from_config,
    _build_installed_app_flow_from_file,
    _credentials_cover_requested_scopes,
    _credentials_from_refresh_token,
    _load_credentials_from_token_file,
    _refresh_credentials,
)
from app.shared.config import Settings
from app.shared.models import PolicyDocument


class CalendarAuthConfigurationError(RuntimeError):
    """Raised when the local Calendar auth setup is incomplete or unsafe."""


class CalendarOAuthAuthenticator:
    """Load, refresh, or create Calendar OAuth credentials for local runs."""

    def __init__(self, *, settings: Settings, policy: PolicyDocument) -> None:
        self.settings = settings
        self.policy = policy
        self.calendar_policy = CalendarConnectorPolicy.from_policy(policy)
        if not self.calendar_policy.allowed_scopes:
            raise CalendarAuthConfigurationError(
                "Policy does not declare any allowed Calendar scopes."
            )

    def authenticate_interactively(self, *, open_browser: bool = True) -> Path:
        self.load_credentials(open_browser=open_browser, allow_interactive=True)
        return self._token_path()

    def build_service(self) -> Any:
        credentials = self.load_credentials()
        return _build_calendar_service(credentials)

    def load_credentials(
        self,
        *,
        open_browser: bool = True,
        allow_interactive: bool = False,
    ) -> Any:
        scopes = self.calendar_policy.allowed_scopes
        token_path = self._token_path()
        credentials = None
        scope_mismatch_detected = False

        if token_path.exists():
            credentials = _load_credentials_from_token_file(token_path, scopes)
            if credentials is not None and not _credentials_cover_requested_scopes(
                credentials,
                scopes,
            ):
                credentials = None
                scope_mismatch_detected = True

        if credentials is not None and getattr(credentials, "valid", False):
            return credentials

        if credentials is not None and getattr(credentials, "expired", False) and getattr(
            credentials, "refresh_token", None
        ):
            _refresh_credentials(credentials)
            if not _credentials_cover_requested_scopes(credentials, scopes):
                credentials = None
                scope_mismatch_detected = True
            else:
                self._persist_credentials(credentials)
                return credentials

        refresh_token = self._env_value(self.calendar_policy.refresh_token_env)
        client_id = self._env_value(self.calendar_policy.client_id_env)
        client_secret = self._env_value(self.calendar_policy.client_secret_env)
        if refresh_token and client_id and client_secret:
            credentials = _credentials_from_refresh_token(
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret,
                scopes=scopes,
            )
            _refresh_credentials(credentials)
            if not _credentials_cover_requested_scopes(credentials, scopes):
                credentials = None
                scope_mismatch_detected = True
            else:
                self._persist_credentials(credentials)
                return credentials

        if not allow_interactive:
            if scope_mismatch_detected:
                raise CalendarAuthConfigurationError(
                    "Existing Calendar credentials do not grant the required scopes for "
                    f"this workflow at {token_path}. Run the explicit Calendar auth "
                    "command again."
                )
            raise CalendarAuthConfigurationError(
                "Interactive Calendar OAuth is disabled for routine workflow runs. "
                f"No usable Calendar token was found at {token_path}. "
                "Run the explicit Calendar auth command for this workflow first."
            )

        flow = self._build_interactive_flow(scopes=scopes)
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            authorization_prompt_message=(
                "Please visit this URL to authorize SAI Calendar access: {url}"
            ),
            success_message="SAI Calendar authentication is complete. You can close this window.",
            open_browser=open_browser,
        )
        if not _credentials_cover_requested_scopes(credentials, scopes):
            raise CalendarAuthConfigurationError(
                "Interactive Calendar OAuth completed, but the granted scopes were still "
                "too narrow for this workflow. Please re-run the auth flow and approve "
                "the requested permissions."
            )
        self._persist_credentials(credentials)
        return credentials

    def auth_summary(self) -> dict[str, str]:
        summary = {
            "token_path": str(self._token_path()),
            "scope_count": str(len(self.calendar_policy.allowed_scopes)),
            "scopes": ", ".join(self.calendar_policy.allowed_scopes),
        }
        credentials_path = self._credentials_path()
        if credentials_path is not None:
            summary["credentials_path"] = str(credentials_path)
        elif self._env_value(self.calendar_policy.client_id_env):
            summary["credential_source"] = "client_id_and_client_secret_env"
        else:
            summary["credential_source"] = "interactive_browser_flow"
        return summary

    def _build_interactive_flow(self, *, scopes: list[str]) -> Any:
        credentials_path = self._credentials_path()
        if credentials_path is not None:
            return _build_installed_app_flow_from_file(credentials_path, scopes)

        client_id = self._env_value(self.calendar_policy.client_id_env)
        client_secret = self._env_value(self.calendar_policy.client_secret_env)
        if not client_id or not client_secret:
            raise CalendarAuthConfigurationError(
                "Set either the policy-approved Calendar credentials file env var or "
                "the approved client ID and client secret env vars before running "
                "the live meeting workflow."
            )

        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": GOOGLE_AUTH_URI,
                "token_uri": GOOGLE_TOKEN_URI,
                "redirect_uris": ["http://127.0.0.1"],
            }
        }
        return _build_installed_app_flow_from_config(client_config, scopes)

    def _credentials_path(self) -> Path | None:
        raw = self._env_value(self.calendar_policy.credentials_path_env)
        if not raw:
            return None
        resolved = self._resolve_path(raw)
        if not resolved.exists():
            raise CalendarAuthConfigurationError(
                f"Calendar credentials file does not exist: {resolved}"
            )
        return resolved

    def _token_path(self) -> Path:
        raw = self._env_value(self.calendar_policy.token_path_env)
        if raw:
            return self._resolve_path(raw)
        return self.settings.tokens_dir / self.calendar_policy.default_token_filename

    def _persist_credentials(self, credentials: Any) -> None:
        token_path = self._token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(str(credentials.to_json()), encoding="utf-8")

    def _resolve_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute():
            return candidate
        return (self.settings.root_dir / candidate).resolve()

    def _env_value(self, env_name: str | None) -> str | None:
        if env_name is None:
            return None
        value = os.getenv(env_name)
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


def _build_calendar_service(credentials: Any) -> Any:
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    return build("calendar", "v3", credentials=credentials, cache_discovery=False)
