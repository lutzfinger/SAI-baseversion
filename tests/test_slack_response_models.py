"""Tests for the Slack Web API response Pydantic models.

Per PRINCIPLES.md §6a — every network response validates against an
explicit schema. These models live in ``app/control_plane/slack_models.py``
and the connector wraps each ``_api_post`` / ``_api_get`` / WebClient
call site through them.

The models use ``extra="ignore"`` (Slack adds new fields without notice
and we don't want to break on additions), but every field we actually
read must be declared with the correct type — drift in a field we
USE surfaces as ``ValidationError`` rather than silent ``None``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.control_plane.slack_models import (
    SlackChatPostMessageResponse,
    SlackConversationsHistoryResponse,
    SlackConversationsListResponse,
    SlackConversationsOpenResponse,
    SlackFilesUploadResponse,
)


# ─── chat.postMessage ─────────────────────────────────────────────────


class TestChatPostMessageResponse:
    def test_canonical_success(self):
        # Real Slack chat.postMessage success shape (trimmed).
        raw = {
            "ok": True,
            "channel": "CABCDEFGHI",
            "ts": "1700000000.000100",
            "message": {"user": "U999", "text": "..."},  # extra — ignored
        }
        resp = SlackChatPostMessageResponse.model_validate(raw)
        assert resp.ok is True
        assert resp.channel == "CABCDEFGHI"
        assert resp.ts == "1700000000.000100"

    def test_error_response(self):
        raw = {"ok": False, "error": "channel_not_found"}
        resp = SlackChatPostMessageResponse.model_validate(raw)
        assert resp.ok is False
        assert resp.error == "channel_not_found"
        assert resp.channel is None

    def test_missing_ok_rejected(self):
        with pytest.raises(ValidationError):
            SlackChatPostMessageResponse.model_validate({"channel": "C1"})

    def test_wrong_type_rejected(self):
        # ts must be str; Slack returning an int would be a contract change.
        with pytest.raises(ValidationError):
            SlackChatPostMessageResponse.model_validate({
                "ok": True, "channel": "C1", "ts": 12345,
            })


# ─── conversations.history ────────────────────────────────────────────


class TestConversationsHistoryResponse:
    def test_messages_default_empty(self):
        resp = SlackConversationsHistoryResponse.model_validate({"ok": True})
        assert resp.messages == []

    def test_messages_pass_through(self):
        raw = {
            "ok": True,
            "messages": [
                {"type": "message", "user": "U1", "text": "hi", "ts": "1.0"},
                {"type": "message", "user": "U2", "text": "yo", "ts": "2.0"},
            ],
            "has_more": False,  # extra — ignored
        }
        resp = SlackConversationsHistoryResponse.model_validate(raw)
        assert len(resp.messages) == 2
        assert resp.messages[0]["user"] == "U1"

    def test_messages_must_be_list(self):
        with pytest.raises(ValidationError):
            SlackConversationsHistoryResponse.model_validate({
                "ok": True, "messages": "not a list",
            })


# ─── conversations.list ───────────────────────────────────────────────


class TestConversationsListResponse:
    def test_canonical_success(self):
        raw = {
            "ok": True,
            "channels": [
                {"id": "C001", "name": "general", "is_archived": False},
                {"id": "C002", "name": "random"},
            ],
            "response_metadata": {"next_cursor": ""},
        }
        resp = SlackConversationsListResponse.model_validate(raw)
        assert len(resp.channels) == 2
        assert resp.channels[0].name == "general"
        assert resp.channels[0].id == "C001"
        assert resp.response_metadata == {"next_cursor": ""}

    def test_paginated_cursor(self):
        raw = {
            "ok": True,
            "channels": [{"id": "C001", "name": "alpha"}],
            "response_metadata": {"next_cursor": "dXNlcjpVMDYxTkZUVDI="},
        }
        resp = SlackConversationsListResponse.model_validate(raw)
        assert resp.response_metadata["next_cursor"] == "dXNlcjpVMDYxTkZUVDI="

    def test_channels_default_empty(self):
        resp = SlackConversationsListResponse.model_validate({"ok": True})
        assert resp.channels == []
        assert resp.response_metadata == {}


# ─── conversations.open ───────────────────────────────────────────────


class TestConversationsOpenResponse:
    def test_canonical_dm_success(self):
        raw = {
            "ok": True,
            "channel": {"id": "D012345", "is_im": True, "user": "U999"},
        }
        resp = SlackConversationsOpenResponse.model_validate(raw)
        assert resp.channel is not None
        assert resp.channel.id == "D012345"

    def test_error_no_channel(self):
        raw = {"ok": False, "error": "user_not_found"}
        resp = SlackConversationsOpenResponse.model_validate(raw)
        assert resp.channel is None
        assert resp.error == "user_not_found"


# ─── files_upload_v2 ──────────────────────────────────────────────────


class TestFilesUploadResponse:
    def test_canonical_success(self):
        raw = {
            "ok": True,
            "file": {
                "id": "F012345",
                "permalink": "https://example.slack.com/files/U/F012345/whatever.mp3",
                "name": "brief.mp3",  # extra — ignored
            },
        }
        resp = SlackFilesUploadResponse.model_validate(raw)
        assert resp.file is not None
        assert resp.file.id == "F012345"
        assert "F012345" in (resp.file.permalink or "")

    def test_missing_file_payload_validates_but_file_is_none(self):
        raw = {"ok": True}
        resp = SlackFilesUploadResponse.model_validate(raw)
        assert resp.file is None
