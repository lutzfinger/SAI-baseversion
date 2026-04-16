"""Narrow Slack connector for posting controlled operational Slack messages."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast
from urllib import parse, request

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.slack_config import SlackConnectorPolicy
from app.shared.models import PolicyDocument


class SlackConnectorError(RuntimeError):
    """Raised when Slack configuration or API usage is invalid."""


class SlackPostConnector:
    """Post narrow operational messages to an allowed Slack channel."""

    def __init__(
        self,
        *,
        policy: PolicyDocument,
        default_channel: str,
    ) -> None:
        self.policy = SlackConnectorPolicy.from_policy(policy)
        self.default_channel = default_channel

    def required_actions(self) -> list[ConnectorAction]:
        return self.required_message_actions()

    def required_message_actions(self) -> list[ConnectorAction]:
        actions = [
            ConnectorAction(
                action="connector.slack.authenticate",
                reason="Posting operational Slack messages requires an explicit Slack bot token.",
            ),
            ConnectorAction(
                action="connector.slack.post_message",
                reason="The workflow posts controlled status or summary messages into Slack.",
            ),
        ]
        if not _looks_like_channel_id(self.default_channel):
            actions.insert(
                1,
                ConnectorAction(
                    action="connector.slack.read_channel_metadata",
                    reason=(
                        "Resolving the configured Slack channel name requires "
                        "channel metadata lookup."
                    ),
                ),
            )
        return actions

    def required_upload_actions(self) -> list[ConnectorAction]:
        actions = [
            ConnectorAction(
                action="connector.slack.authenticate",
                reason="Uploading audio to Slack requires an explicit Slack bot token.",
            ),
            ConnectorAction(
                action="connector.slack.upload_file",
                reason=(
                    "The workflow uploads a generated audio briefing into an allowed Slack channel."
                ),
            ),
        ]
        if not _looks_like_channel_id(self.default_channel):
            actions.insert(
                1,
                ConnectorAction(
                    action="connector.slack.read_channel_metadata",
                    reason=(
                        "Resolving the configured Slack channel name requires "
                        "channel metadata lookup."
                    ),
                ),
            )
        return actions

    def describe(self) -> ConnectorDescriptor:
        return ConnectorDescriptor(
            component_name="connector.slack-post",
            source_details={
                "default_channel": self.default_channel,
                "allowed_channel_names": list(self.policy.allowed_channel_names),
                "allowed_channel_ids": list(self.policy.allowed_channel_ids),
                "api_base_url": self.policy.api_base_url,
            },
        )

    def post_message(
        self,
        *,
        text: str,
        channel: str | None = None,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        token = self._bot_token()
        configured_channel = channel or self.default_channel
        channel_id = self._resolve_channel_id(configured_channel, token=token)
        payload = {
            "channel": channel_id,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts is not None:
            payload["thread_ts"] = thread_ts
        if blocks:
            payload["blocks"] = blocks
        response = self._api_post("chat.postMessage", payload=payload, token=token)
        if not response.get("ok", False):
            raise SlackConnectorError(
                f"Slack chat.postMessage failed: {response.get('error', 'unknown_error')}"
            )
        return {
            "channel": str(response.get("channel", channel_id)),
            "ts": str(response.get("ts", "")),
        }

    def open_direct_message_channel(self, *, user_id: str) -> str:
        """Resolve or open the direct-message channel for one allowed user."""

        token = self._bot_token()
        return self._open_direct_message_channel(user_id=user_id, token=token)

    def list_messages(
        self,
        *,
        channel: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Read recent message history for one allowed channel or DM."""

        token = self._bot_token()
        configured_channel = channel or self.default_channel
        channel_id = self._resolve_channel_id(configured_channel, token=token)
        query = parse.urlencode({"channel": channel_id, "limit": max(1, min(limit, 200))})
        response = self._api_get(f"conversations.history?{query}", token=token)
        if not response.get("ok", False):
            raise SlackConnectorError(
                f"Slack conversations.history failed: {response.get('error', 'unknown_error')}"
            )
        messages = response.get("messages", [])
        if not isinstance(messages, list):
            raise SlackConnectorError("Slack conversations.history returned malformed messages.")
        return [item for item in messages if isinstance(item, dict)]

    def upload_file(
        self,
        *,
        file_path: str | Path,
        title: str,
        channel: str | None = None,
        initial_comment: str | None = None,
        thread_ts: str | None = None,
    ) -> dict[str, str]:
        token = self._bot_token()
        configured_channel = channel or self.default_channel
        channel_id = self._resolve_channel_id(configured_channel, token=token)
        path = Path(file_path)
        client = self._web_client(token)
        try:
            response = client.files_upload_v2(
                channel=channel_id,
                file=path,
                filename=path.name,
                title=title,
                initial_comment=initial_comment,
                thread_ts=thread_ts,
            )
        except SlackApiError as exc:
            error_message = exc.response.get("error", "slack_api_error")
            raise SlackConnectorError(f"Slack files_upload_v2 failed: {error_message}") from exc
        file_payload: dict[str, Any] = cast(dict[str, Any], response.get("file", {}))
        if not isinstance(file_payload, dict):
            raise SlackConnectorError("Slack file upload returned a malformed file payload.")
        return {
            "channel": channel_id,
            "file_id": str(file_payload.get("id", "")),
            "title": title,
            "permalink": str(file_payload.get("permalink", "")),
        }

    def _resolve_channel_id(self, channel: str, *, token: str) -> str:
        if _looks_like_channel_id(channel):
            channel_id = channel.strip()
            self._assert_allowed(channel_name=None, channel_id=channel_id)
            return channel_id
        if _looks_like_user_id(channel):
            return self._open_direct_message_channel(user_id=channel.strip(), token=token)

        target_name = channel.strip().lstrip("#").lower()
        self._assert_allowed(channel_name=target_name, channel_id=None)
        cursor = ""
        while True:
            query = parse.urlencode(
                {
                    "limit": 1000,
                    "exclude_archived": "true",
                    "types": "public_channel,private_channel",
                    **({"cursor": cursor} if cursor else {}),
                }
            )
            response = self._api_get(
                f"conversations.list?{query}",
                token=token,
            )
            if not response.get("ok", False):
                raise SlackConnectorError(
                    f"Slack conversations.list failed: {response.get('error', 'unknown_error')}"
                )
            channels = response.get("channels", [])
            if isinstance(channels, list):
                for item in channels:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("name", "")).strip().lower() == target_name:
                        channel_id = str(item.get("id", "")).strip()
                        self._assert_allowed(channel_name=target_name, channel_id=channel_id)
                        return channel_id
            metadata = response.get("response_metadata", {})
            if not isinstance(metadata, dict):
                break
            cursor = str(metadata.get("next_cursor", "")).strip()
            if not cursor:
                break
        raise SlackConnectorError(f"Slack channel not found or not accessible: {channel}")

    def _open_direct_message_channel(self, *, user_id: str, token: str) -> str:
        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            raise SlackConnectorError("Slack direct message target user is empty.")
        if not self.policy.allow_direct_messages:
            raise SlackConnectorError("Slack direct messages are not allowed by policy.")
        if self.policy.allowed_user_ids and normalized_user_id not in self.policy.allowed_user_ids:
            raise SlackConnectorError("Slack direct message target user is not allowed.")
        response = self._api_post(
            "conversations.open",
            payload={"users": normalized_user_id},
            token=token,
        )
        if not response.get("ok", False):
            raise SlackConnectorError(
                f"Slack conversations.open failed: {response.get('error', 'unknown_error')}"
            )
        channel = response.get("channel", {})
        if not isinstance(channel, dict):
            raise SlackConnectorError(
                "Slack conversations.open returned a malformed channel payload."
            )
        channel_id = str(channel.get("id", "")).strip()
        if not _looks_like_channel_id(channel_id):
            raise SlackConnectorError(
                "Slack conversations.open did not return a valid direct-message channel."
            )
        self._assert_allowed(channel_name=None, channel_id=channel_id)
        return channel_id

    def _assert_allowed(self, *, channel_name: str | None, channel_id: str | None) -> None:
        if channel_id and channel_id.startswith("D") and self.policy.allow_direct_messages:
            return
        if (
            channel_id
            and self.policy.allowed_channel_ids
            and channel_id in self.policy.allowed_channel_ids
        ):
            return
        if (
            channel_name
            and self.policy.allowed_channel_names
            and channel_name in self.policy.allowed_channel_names
        ):
            return
        if not self.policy.allowed_channel_ids and not self.policy.allowed_channel_names:
            raise SlackConnectorError(
                "Slack policy must define at least one allowed channel name or ID."
            )
        raise SlackConnectorError("Configured Slack channel is not allowed by policy.")

    def _bot_token(self) -> str:
        value = _normalize_env_assignment_secret(os.getenv(self.policy.bot_token_env, ""))
        if not value:
            raise SlackConnectorError(
                f"Missing Slack bot token env var: {self.policy.bot_token_env}"
            )
        if not value.startswith("xoxb-"):
            raise SlackConnectorError(
                "Slack bot token env var "
                f"{self.policy.bot_token_env} does not look like a bot token."
            )
        return value

    def _api_get(self, path: str, *, token: str) -> dict[str, Any]:
        req = request.Request(
            f"{self.policy.api_base_url}/{path}",
            headers={
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )
        with request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise SlackConnectorError("Slack API returned a non-object response.")
        return cast(dict[str, Any], payload)

    def _api_post(self, path: str, *, payload: dict[str, Any], token: str) -> dict[str, Any]:
        raw = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.policy.api_base_url}/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=raw,
            method="POST",
        )
        with request.urlopen(req, timeout=15) as response:
            parsed = json.loads(response.read().decode("utf-8"))
        if not isinstance(parsed, dict):
            raise SlackConnectorError("Slack API returned a non-object response.")
        return cast(dict[str, Any], parsed)

    def _web_client(self, token: str) -> WebClient:
        return WebClient(
            token=token,
            base_url=f"{self.policy.api_base_url.rstrip('/')}/",
            timeout=30,
        )


def _looks_like_channel_id(value: str) -> bool:
    stripped = value.strip()
    return len(stripped) >= 9 and stripped[:1] in {"C", "G", "D"} and stripped.isalnum()


def _looks_like_user_id(value: str) -> bool:
    stripped = value.strip()
    return len(stripped) >= 9 and stripped[:1] == "U" and stripped.isalnum()


def _normalize_env_assignment_secret(value: str) -> str:
    stripped = value.strip()
    if "=" not in stripped:
        return stripped
    key, _, remainder = stripped.partition("=")
    if not key:
        return stripped
    normalized_key = key.replace("_", "")
    if not normalized_key.isalnum() or key.upper() != key:
        return stripped
    return remainder.strip()
