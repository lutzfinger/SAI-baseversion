"""Tests for the channel allowed-discussion registry (#16i)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.runtime import channel_registry


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    channel_registry.reload()
    yield
    channel_registry.reload()


def _swap_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict) -> None:
    target = tmp_path / "channel_allowed_discussion.yaml"
    target.write_text(yaml.safe_dump(body), encoding="utf-8")
    monkeypatch.setattr(channel_registry, "REGISTRY_PATH", target)
    channel_registry.reload()


def test_known_channel_with_topic_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _swap_registry(tmp_path, monkeypatch, {
        "channels": {
            "test-eval": {
                "description": "test channel",
                "allowed_topics": [
                    {"kind": "rule_change", "risk_class": "low", "tools": []},
                ],
            },
        },
    })

    assert channel_registry.is_allowed("test-eval", "rule_change") is True


def test_unknown_channel_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _swap_registry(tmp_path, monkeypatch, {"channels": {}})

    assert channel_registry.is_allowed("random-channel", "rule_change") is False


def test_known_channel_unknown_topic_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _swap_registry(tmp_path, monkeypatch, {
        "channels": {
            "test-eval": {
                "allowed_topics": [
                    {"kind": "rule_change", "risk_class": "low", "tools": []},
                ],
            },
        },
    })

    assert channel_registry.is_allowed("test-eval", "off_topic") is False


def test_forbidden_topic_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _swap_registry(tmp_path, monkeypatch, {
        "channels": {
            "test-eval": {
                "allowed_topics": [
                    {"kind": "blocked_kind", "risk_class": "forbidden", "tools": []},
                ],
            },
        },
    })

    assert channel_registry.is_allowed("test-eval", "blocked_kind") is False


def test_channel_name_normalisation_strips_leading_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _swap_registry(tmp_path, monkeypatch, {
        "channels": {
            "test-eval": {
                "allowed_topics": [
                    {"kind": "rule_change", "risk_class": "low", "tools": []},
                ],
            },
        },
    })

    # Same channel referenced with and without '#' should both work.
    assert channel_registry.is_allowed("test-eval", "rule_change") is True
    assert channel_registry.is_allowed("#test-eval", "rule_change") is True


def test_refusal_message_lists_topics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _swap_registry(tmp_path, monkeypatch, {
        "channels": {
            "test-eval": {
                "description": "test channel",
                "allowed_topics": [
                    {
                        "kind": "rule_change",
                        "risk_class": "low",
                        "tools": [],
                        "description": "Add or remove a rule",
                    },
                ],
            },
        },
    })

    msg = channel_registry.refusal_message("test-eval")
    assert "rule_change" in msg
    assert "Add or remove a rule" in msg


def test_refusal_message_for_unregistered_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _swap_registry(tmp_path, monkeypatch, {"channels": {}})

    msg = channel_registry.refusal_message("random-channel")
    assert "isn't registered" in msg
    assert "random-channel" in msg


def test_real_registry_has_sai_eval() -> None:
    """The shipped channel_allowed_discussion.yaml MUST register sai-eval."""

    spec = channel_registry.get_channel("sai-eval")
    assert spec is not None
    kinds = spec.topic_kinds()
    assert "classifier_rule_change" in kinds
    assert "llm_example_addition" in kinds
    assert "query_label_state" in kinds
