"""Channel allowed-discussion registry accessor (PRINCIPLES.md §16i).

Each guarded interface declares — in ``config/channel_allowed_discussion.yaml`` —
what topic kinds it can discuss + what risk class each topic carries.
The runtime checks the registry on EVERY message before invoking any
tool. Topics not in the allowlist get a friendly refusal (per #16e —
never silent).

Default for new channels: empty allowlist (refuse everything). Adding
a topic requires an explicit registry entry.

This module is intentionally tiny and side-effect-free. The slack_bot
imports it; future HTTP / web surfaces import it. Risk-class →
gating-policy mapping lives in the consumer (the bot decides whether
to also invoke the second-opinion gate based on risk_class).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.shared.config import REPO_ROOT


REGISTRY_PATH: Path = REPO_ROOT / "config" / "channel_allowed_discussion.yaml"


RiskClass = Literal["minimal", "low", "medium", "high", "forbidden"]


class TopicSpec(BaseModel):
    """One allowed topic on one channel."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    risk_class: RiskClass
    tools: list[str] = Field(default_factory=list)
    description: str = ""


class ChannelSpec(BaseModel):
    """One channel's allowlist."""

    model_config = ConfigDict(extra="forbid")

    channel_name: str
    description: str = ""
    allowed_topics: list[TopicSpec] = Field(default_factory=list)

    def is_topic_allowed(self, kind: str) -> bool:
        return any(t.kind == kind for t in self.allowed_topics)

    def get_topic(self, kind: str) -> Optional[TopicSpec]:
        for t in self.allowed_topics:
            if t.kind == kind:
                return t
        return None

    def topic_kinds(self) -> list[str]:
        return [t.kind for t in self.allowed_topics]


@lru_cache(maxsize=1)
def _load() -> dict[str, ChannelSpec]:
    if not REGISTRY_PATH.exists():
        return {}
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    chans = raw.get("channels", {}) or {}
    if not isinstance(chans, dict):
        return {}
    out: dict[str, ChannelSpec] = {}
    for name, body in chans.items():
        if not isinstance(body, dict):
            continue
        body = dict(body)
        body.setdefault("channel_name", str(name))
        out[str(name)] = ChannelSpec(**body)
    return out


def reload() -> None:
    """Force re-read of the registry. Mostly for tests."""
    _load.cache_clear()


def get_channel(channel_name: str) -> Optional[ChannelSpec]:
    """Look up a channel. Returns None if the channel is not registered.

    NOT raising here is deliberate: callers receive None and decide
    whether the right response is "refuse, channel unregistered" vs
    "the message arrived on a system channel and we ignore it".
    """
    # Channel names in Slack carry the leading hash mark in some contexts
    # but not others; strip it so a registry entry like ``sai-eval``
    # matches both with-hash and bare forms.
    normalised = channel_name.lstrip("#")
    return _load().get(normalised)


def all_channels() -> dict[str, ChannelSpec]:
    """Snapshot for sai-health + audit."""
    return dict(_load())


def is_allowed(channel_name: str, topic_kind: str) -> bool:
    """True iff the channel is registered AND the topic_kind is in its allowlist.

    Convenience wrapper for the bot's per-message gate. False on any
    of: channel unregistered, topic not in allowlist, topic risk_class
    is "forbidden".
    """
    spec = get_channel(channel_name)
    if spec is None:
        return False
    topic = spec.get_topic(topic_kind)
    if topic is None:
        return False
    if topic.risk_class == "forbidden":
        return False
    return True


def refusal_message(channel_name: str) -> str:
    """Build the friendly refusal text per #16e for an unregistered or
    out-of-scope topic on ``channel_name``.

    Sources the topic list from the registry so the message + the
    capability stay in sync as the channel grows new topics.
    """
    spec = get_channel(channel_name)
    if spec is None:
        return (
            f"This channel (#{channel_name.lstrip('#')}) isn't registered for "
            f"any of my workflows. I'll stay quiet here. If you want me to "
            f"do something on this channel, add an entry to "
            f"config/channel_allowed_discussion.yaml."
        )
    if not spec.allowed_topics:
        return (
            f"#{channel_name.lstrip('#')} doesn't have any allowed topics yet. "
            f"Add an `allowed_topics` entry in config/channel_allowed_discussion.yaml "
            f"to enable a workflow here."
        )
    bullet_lines = "\n".join(
        f"  • `{t.kind}` — {t.description or 'no description'}"
        for t in spec.allowed_topics
    )
    return (
        f"That's outside what I do here on #{channel_name.lstrip('#')}. "
        f"This channel handles:\n{bullet_lines}"
    )
