"""Typed Google Calendar connector policy helpers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PolicyDocument


class CalendarConnectorPolicy(BaseModel):
    """Allowed Calendar credential keys and scopes derived from policy."""

    model_config = ConfigDict(extra="forbid")

    credentials_path_env: str | None = None
    client_id_env: str
    client_secret_env: str
    refresh_token_env: str | None = None
    token_path_env: str | None = None
    default_token_filename: str = "calendar_token.json"
    allowed_scopes: list[str] = Field(default_factory=list)

    @classmethod
    def from_policy(cls, policy: PolicyDocument) -> CalendarConnectorPolicy:
        calendar_config = policy.calendar
        env_keys = calendar_config.get("allowed_env_keys", {})
        return cls(
            credentials_path_env=_optional_string(env_keys.get("credentials_path")),
            client_id_env=str(env_keys.get("client_id", "SAI_GMAIL_CLIENT_ID")),
            client_secret_env=str(env_keys.get("client_secret", "SAI_GMAIL_CLIENT_SECRET")),
            refresh_token_env=_optional_string(env_keys.get("refresh_token")),
            token_path_env=_optional_string(env_keys.get("token_path")),
            default_token_filename=str(
                calendar_config.get("default_token_filename", "calendar_token.json")
            ),
            allowed_scopes=[str(scope) for scope in calendar_config.get("allowed_scopes", [])],
        )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
