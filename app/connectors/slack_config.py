"""Typed Slack connector policy helpers."""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PolicyDocument


class SlackConnectorPolicy(BaseModel):
    """Allowed Slack credential keys and channel restrictions derived from policy."""

    model_config = ConfigDict(extra="forbid")

    bot_token_env: str = "SAI_SLACK_BOT_TOKEN"
    app_token_env: str = "SAI_SLACK_APP_TOKEN"
    signing_secret_env: str = "SAI_SLACK_SIGNING_SECRET"
    allowed_user_ids_env: str = "SAI_SLACK_ALLOWED_USER_IDS"
    allowed_channel_names: list[str] = Field(default_factory=list)
    allowed_channel_ids: list[str] = Field(default_factory=list)
    allowed_user_ids: list[str] = Field(default_factory=list)
    allow_direct_messages: bool = False
    api_base_url: str = "https://slack.com/api"

    @classmethod
    def from_policy(cls, policy: PolicyDocument) -> SlackConnectorPolicy:
        slack_config = policy.slack
        env_keys = slack_config.get("allowed_env_keys", {})
        allowed_user_ids_env = str(
            slack_config.get("allowed_user_ids_env", "SAI_SLACK_ALLOWED_USER_IDS")
        ).strip()
        configured_allowed_user_ids = [
            str(value).strip()
            for value in slack_config.get("allowed_user_ids", [])
            if str(value).strip()
        ]
        env_allowed_user_ids = _split_env_list(os.getenv(allowed_user_ids_env, ""))
        return cls(
            bot_token_env=str(env_keys.get("bot_token", "SAI_SLACK_BOT_TOKEN")),
            app_token_env=str(env_keys.get("app_token", "SAI_SLACK_APP_TOKEN")),
            signing_secret_env=str(env_keys.get("signing_secret", "SAI_SLACK_SIGNING_SECRET")),
            allowed_user_ids_env=allowed_user_ids_env or "SAI_SLACK_ALLOWED_USER_IDS",
            allowed_channel_names=[
                _normalize_channel_name(value)
                for value in slack_config.get("allowed_channel_names", [])
                if _normalize_channel_name(value)
            ],
            allowed_channel_ids=[
                str(value).strip()
                for value in slack_config.get("allowed_channel_ids", [])
                if str(value).strip()
            ],
            allowed_user_ids=list(
                dict.fromkeys(configured_allowed_user_ids + env_allowed_user_ids)
            ),
            allow_direct_messages=bool(slack_config.get("allow_direct_messages", False)),
            api_base_url=str(slack_config.get("api_base_url", "https://slack.com/api")).rstrip("/"),
        )


def _normalize_channel_name(value: object) -> str:
    return str(value).strip().lstrip("#").lower()


def _split_env_list(raw: str) -> list[str]:
    return [value.strip() for value in raw.replace(";", ",").split(",") if value.strip()]
