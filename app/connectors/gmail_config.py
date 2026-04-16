"""Typed Gmail connector policy helpers.

The live Gmail path is still driven by policy files, not hidden connector code.
This module is where the project defines which env vars and scopes are allowed
for official Gmail access.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PolicyDocument


class GmailConnectorPolicy(BaseModel):
    """Allowed Gmail credential keys and scopes derived from policy."""

    model_config = ConfigDict(extra="forbid")

    credentials_path_env: str | None = None
    client_id_env: str
    client_secret_env: str
    refresh_token_env: str | None = None
    token_path_env: str | None = None
    extra_token_paths_env: str | None = None
    default_token_filename: str = "gmail_token.json"
    allowed_scopes: list[str] = Field(default_factory=list)
    allowed_mailbox_domains: list[str] = Field(default_factory=list)
    allowed_mailbox_accounts: list[str] = Field(default_factory=list)

    @classmethod
    def from_policy(cls, policy: PolicyDocument) -> GmailConnectorPolicy:
        gmail_config = policy.gmail
        env_keys = gmail_config.get("allowed_env_keys", {})
        return cls(
            credentials_path_env=_optional_string(env_keys.get("credentials_path")),
            client_id_env=str(env_keys.get("client_id", "SAI_GMAIL_CLIENT_ID")),
            client_secret_env=str(env_keys.get("client_secret", "SAI_GMAIL_CLIENT_SECRET")),
            refresh_token_env=_optional_string(env_keys.get("refresh_token")),
            token_path_env=_optional_string(env_keys.get("token_path")),
            extra_token_paths_env=_optional_string(env_keys.get("extra_token_paths")),
            default_token_filename=str(
                gmail_config.get("default_token_filename", "gmail_token.json")
            ),
            allowed_scopes=[str(scope) for scope in gmail_config.get("allowed_scopes", [])],
            allowed_mailbox_domains=[
                str(domain).strip().lower()
                for domain in gmail_config.get("allowed_mailbox_domains", [])
                if str(domain).strip()
            ],
            allowed_mailbox_accounts=[
                str(account).strip().lower()
                for account in gmail_config.get("allowed_mailbox_accounts", [])
                if str(account).strip()
            ],
        )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
