"""Official Gmail OAuth helper for local desktop testing.

This module keeps Gmail authentication explicit and local-first:
- OAuth scopes are read from checked-in policy
- credentials come from approved env vars
- the token is stored on disk only on the local machine
- browser-based auth happens only when the operator initiates it
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from app.connectors.gmail_config import GmailConnectorPolicy
from app.shared.config import Settings
from app.shared.models import PolicyDocument

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GmailAuthConfigurationError(RuntimeError):
    """Raised when the local Gmail auth setup is incomplete or unsafe."""


class GmailOAuthAuthenticator:
    """Load, refresh, or create Gmail OAuth credentials for local runs."""

    def __init__(
        self,
        *,
        settings: Settings,
        policy: PolicyDocument,
        token_path_override: Path | None = None,
    ) -> None:
        self.settings = settings
        self.policy = policy
        self.gmail_policy = GmailConnectorPolicy.from_policy(policy)
        self.token_path_override = token_path_override
        if not self.gmail_policy.allowed_scopes:
            raise GmailAuthConfigurationError("Policy does not declare any allowed Gmail scopes.")

    def authenticate_interactively(self, *, open_browser: bool = True) -> Path:
        """Ensure a valid token exists locally, launching the browser if needed."""

        self.load_credentials(open_browser=open_browser, allow_interactive=True)
        return self._token_path()

    def build_service(self) -> Any:
        """Return an authenticated Gmail API client."""

        credentials = self.load_credentials()
        return _build_gmail_service(credentials)

    def load_credentials(
        self,
        *,
        open_browser: bool = True,
        allow_interactive: bool = False,
    ) -> Any:
        """Return valid Gmail credentials, refreshing or bootstrapping as needed."""

        scopes = self.gmail_policy.allowed_scopes
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
            account = self._assert_allowed_mailbox(credentials)
            self._persist_credentials(credentials, account=account)
            return credentials

        if (
            credentials is not None
            and getattr(credentials, "expired", False)
            and getattr(credentials, "refresh_token", None)
        ):
            _refresh_credentials(credentials)
            if not _credentials_cover_requested_scopes(credentials, scopes):
                credentials = None
                scope_mismatch_detected = True
            else:
                account = self._assert_allowed_mailbox(credentials)
                self._persist_credentials(credentials, account=account)
                return credentials

        refresh_token = self._env_value(self.gmail_policy.refresh_token_env)
        client_id = self._env_value(self.gmail_policy.client_id_env)
        client_secret = self._env_value(self.gmail_policy.client_secret_env)
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
                account = self._assert_allowed_mailbox(credentials)
                self._persist_credentials(credentials, account=account)
                return credentials

        if not allow_interactive:
            if scope_mismatch_detected:
                raise GmailAuthConfigurationError(
                    "Existing Gmail credentials do not grant the required scopes for this "
                    f"workflow at {token_path}. Run the explicit Gmail auth command again."
                )
            raise GmailAuthConfigurationError(
                "Interactive Gmail OAuth is disabled for routine workflow runs. "
                f"No usable Gmail token was found at {token_path}. "
                "Run the explicit Gmail auth command for this workflow first."
            )

        flow = self._build_interactive_flow(scopes=scopes)
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            authorization_prompt_message=(
                "Please visit this URL to authorize SAI Gmail access: {url}"
            ),
            success_message="SAI Gmail authentication is complete. You can close this window.",
            open_browser=open_browser,
        )
        if not _credentials_cover_requested_scopes(credentials, scopes):
            raise GmailAuthConfigurationError(
                "Interactive Gmail OAuth completed, but the granted scopes were still too "
                "narrow for this workflow. Please re-run the auth flow and approve the "
                "requested permissions."
            )
        account = self._assert_allowed_mailbox(credentials)
        self._persist_credentials(credentials, account=account)
        return credentials

    def auth_summary(self) -> dict[str, str]:
        """Return safe operator-facing details about the configured auth path."""

        summary = {
            "token_path": str(self._token_path()),
            "scope_count": str(len(self.gmail_policy.allowed_scopes)),
            "scopes": ", ".join(self.gmail_policy.allowed_scopes),
        }
        token_path = self._token_path()
        if token_path.exists():
            try:
                token_payload = json.loads(token_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                token_payload = {}
            account = token_payload.get("account")
            if isinstance(account, str) and account.strip():
                summary["account"] = account.strip()
        credentials_path = self._credentials_path()
        if credentials_path is not None:
            summary["credentials_path"] = str(credentials_path)
        elif self._env_value(self.gmail_policy.client_id_env):
            summary["credential_source"] = "client_id_and_client_secret_env"
        else:
            summary["credential_source"] = "interactive_browser_flow"
        return summary

    def _build_interactive_flow(self, *, scopes: list[str]) -> Any:
        credentials_path = self._credentials_path()
        if credentials_path is not None:
            return _build_installed_app_flow_from_file(credentials_path, scopes)

        client_id = self._env_value(self.gmail_policy.client_id_env)
        client_secret = self._env_value(self.gmail_policy.client_secret_env)
        if not client_id or not client_secret:
            raise GmailAuthConfigurationError(
                "Set either the policy-approved Gmail credentials file env var or "
                "the approved client ID and client secret env vars before running "
                "the live Gmail workflow."
            )

        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": GOOGLE_AUTH_URI,
                "token_uri": GOOGLE_TOKEN_URI,
                # Loopback redirect URIs are what the official desktop flow uses.
                "redirect_uris": ["http://127.0.0.1"],
            }
        }
        return _build_installed_app_flow_from_config(client_config, scopes)

    def _credentials_path(self) -> Path | None:
        raw = self._env_value(self.gmail_policy.credentials_path_env)
        if not raw:
            return None
        resolved = self._resolve_path(raw)
        if not resolved.exists():
            raise GmailAuthConfigurationError(f"Gmail credentials file does not exist: {resolved}")
        return resolved

    def _token_path(self) -> Path:
        if self.token_path_override is not None:
            return self.token_path_override.expanduser()
        raw = self._env_value(self.gmail_policy.token_path_env)
        if raw:
            return self._resolve_path(raw)
        return self.settings.logs_dir / self.gmail_policy.default_token_filename

    def _persist_credentials(self, credentials: Any, *, account: str | None = None) -> None:
        token_path = self._token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(str(credentials.to_json()))
        if account:
            payload["account"] = account
        token_path.write_text(json.dumps(payload), encoding="utf-8")

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

    def _assert_allowed_mailbox(self, credentials: Any) -> str:
        account = _fetch_authenticated_account(credentials).strip().lower()
        if not account:
            raise GmailAuthConfigurationError(
                "Authenticated Gmail account could not be determined."
            )
        if self._mailbox_allowed(account):
            return account
        allowed_accounts = ", ".join(self.gmail_policy.allowed_mailbox_accounts) or "(none)"
        allowed_domains = ", ".join(self.gmail_policy.allowed_mailbox_domains) or "(none)"
        raise GmailAuthConfigurationError(
            "Authenticated Gmail account is not allowed for SAI. "
            f"Got: {account}. Allowed accounts: {allowed_accounts}. "
            f"Allowed domains: {allowed_domains}."
        )

    def _mailbox_allowed(self, account: str) -> bool:
        normalized = account.strip().lower()
        if (
            not self.gmail_policy.allowed_mailbox_accounts
            and not self.gmail_policy.allowed_mailbox_domains
        ):
            return True
        if normalized in self.gmail_policy.allowed_mailbox_accounts:
            return True
        if "@" not in normalized:
            return False
        domain = normalized.rsplit("@", 1)[1]
        return domain in self.gmail_policy.allowed_mailbox_domains


def _load_credentials_from_token_file(token_path: Path, scopes: list[str]) -> Any:
    from google.oauth2.credentials import Credentials

    payload = json.loads(token_path.read_text(encoding="utf-8"))
    granted_scopes = payload.get("scopes")
    if isinstance(granted_scopes, str):
        granted_scopes = [granted_scopes]
    if not isinstance(granted_scopes, list):
        granted_scopes = []
    credentials = Credentials.from_authorized_user_info(payload, scopes)  # type: ignore[no-untyped-call]
    if granted_scopes:
        credentials._sai_granted_scopes = list(granted_scopes)
    return credentials


def _credentials_cover_requested_scopes(credentials: Any, scopes: list[str]) -> bool:
    requested = {scope.strip() for scope in scopes if scope and scope.strip()}
    granted_scopes = getattr(credentials, "granted_scopes", None)
    if granted_scopes is None:
        granted_scopes = getattr(credentials, "_sai_granted_scopes", None)
    if granted_scopes is None:
        granted_scopes = getattr(credentials, "scopes", None)
    if not granted_scopes:
        return True
    granted = {str(scope).strip() for scope in granted_scopes if str(scope).strip()}
    return requested.issubset(granted)


def _credentials_from_refresh_token(
    *, refresh_token: str, client_id: str, client_secret: str, scopes: list[str]
) -> Any:
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )  # type: ignore[no-untyped-call]


def _refresh_credentials(credentials: Any) -> None:
    from google.auth.transport.requests import Request

    credentials.refresh(Request())


def _build_installed_app_flow_from_file(credentials_path: Path, scopes: list[str]) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

    return InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)


def _build_installed_app_flow_from_config(client_config: dict[str, Any], scopes: list[str]) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow

    return InstalledAppFlow.from_client_config(client_config, scopes)


def _build_gmail_service(credentials: Any) -> Any:
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _fetch_authenticated_account(credentials: Any) -> str:
    service = _build_gmail_service(credentials)
    profile = service.users().getProfile(userId="me").execute()
    email_address = profile.get("emailAddress")
    if not isinstance(email_address, str):
        return ""
    return email_address


def load_client_config(credentials_path: Path) -> dict[str, Any]:
    """Load a client secrets JSON file for tests or setup validation."""

    return cast(dict[str, Any], json.loads(credentials_path.read_text(encoding="utf-8")))
