"""Best-effort Slack notices when SAI is about to request 1Password access."""

from __future__ import annotations

import os
import platform
import shlex
from pathlib import Path

from app.connectors.slack import SlackConnectorError, SlackPostConnector
from app.control_plane.loaders import PolicyStore
from app.shared.config import Settings


def build_onepassword_access_message(
    *,
    command: list[str],
    cwd: Path,
    hostname: str | None = None,
) -> str:
    """Render a short Slack message explaining which process wants 1Password."""

    rendered_command = shlex.join(command).strip() or "(no command provided)"
    return "\n".join(
        [
            "SAI is requesting 1Password access.",
            f"Process: `{rendered_command}`",
            f"Workspace: `{cwd}`",
            f"Host: `{hostname or platform.node() or 'unknown-host'}`",
        ]
    )


def post_onepassword_access_notice(
    *,
    settings: Settings,
    command: list[str],
    cwd: Path,
) -> dict[str, str] | None:
    """Post a best-effort pre-unlock notice to the dedicated Slack channel."""

    _promote_preunlock_bot_token_if_needed()
    policy = PolicyStore(settings.policies_dir).load("slack_bot.yaml")
    connector = SlackPostConnector(
        policy=policy,
        default_channel=settings.slack_onepassword_channel,
    )
    text = build_onepassword_access_message(command=command, cwd=cwd)
    try:
        return connector.post_message(
            text=text,
            channel=settings.slack_onepassword_channel,
        )
    except SlackConnectorError:
        return None


def _promote_preunlock_bot_token_if_needed() -> None:
    """Allow a plain-env Slack token just for pre-unlock 1Password notices."""

    if os.getenv("SAI_SLACK_BOT_TOKEN", "").strip():
        return
    fallback = os.getenv("SAI_SLACK_PREUNLOCK_BOT_TOKEN", "").strip()
    if fallback:
        os.environ["SAI_SLACK_BOT_TOKEN"] = fallback
