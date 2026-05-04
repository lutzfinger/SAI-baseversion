"""Tests for the sai-eval agent's tool surface (LangChain edition).

Each tool is exercised with a stubbed Gmail authenticator + service so
tests stay fast + offline. We verify:

  * Read-only tools return the expected shapes / honour caps
  * list_gmail_labels returns ANY label (no L1/* gate)
  * read_thread identifies the first external sender
  * propose_* tools refuse labels not in Gmail
  * propose_classifier_rule and propose_llm_example each carry the
    correct target_system + eval_dataset_to_update markers
  * Internal_domains config from SAI_INTERNAL_DOMAINS env var
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from app.agents.tools import (
    BUCKET_NAME_PATTERN,
    REGISTERED_TOOL_SPECS,
    ToolContext,
    _internal_domains,
    _is_external_sender,
    build_tools,
)


# ─── helpers ───────────────────────────────────────────────────────────


def _ctx(
    tmp_path: Path,
    *,
    gmail_labels: list[str] | None = None,
) -> ToolContext:
    auth = MagicMock()
    service = MagicMock()
    auth.build_service.return_value = service
    label_items = [
        {"id": f"id_{n}", "name": n} for n in (gmail_labels or [])
    ]
    service.users().labels().list().execute.return_value = {
        "labels": label_items,
    }
    ctx = ToolContext(
        proposed_by="U999",
        source_text="alex should be customers",
        proposed_dir=tmp_path / "proposed",
        gmail_authenticator=auth,
        cache={},
    )
    return ctx


def _tool_by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(name)


def _fake_email_message(
    *, message_id="abcdefghij1234567890", from_email="alex@example.com",
    from_name="Alex", subject="Re: thoughts", snippet="hey",
    received_at=None, thread_id=None,
):
    from app.workers.email_models import EmailMessage
    return EmailMessage(
        message_id=message_id,
        thread_id=thread_id or message_id,
        from_email=from_email,
        from_name=from_name,
        to=["operator@example.com"],
        subject=subject,
        snippet=snippet,
        body_excerpt=snippet,
        received_at=received_at or datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
    )


@pytest.fixture
def patched_connector(monkeypatch):
    captured = {}

    def fake_init(self, *, authenticator, user_id, query, label_ids, max_results):
        captured["query"] = query
        captured["max_results"] = max_results
        self._test_results = getattr(authenticator, "_test_results", [])
        self._test_thread = getattr(authenticator, "_test_thread", [])

    def fake_fetch(self):
        return self._test_results

    def fake_fetch_thread(self, *, thread_id):
        captured["thread_id"] = thread_id
        return self._test_thread

    import app.connectors.gmail as gmail_module
    monkeypatch.setattr(gmail_module.GmailAPIConnector, "__init__", fake_init)
    monkeypatch.setattr(gmail_module.GmailAPIConnector, "fetch_messages", fake_fetch)
    monkeypatch.setattr(
        gmail_module.GmailAPIConnector, "fetch_thread_messages", fake_fetch_thread,
    )
    return captured


# ─── search_gmail ─────────────────────────────────────────────────────


class TestSearchGmail:
    def test_returns_summaries(self, tmp_path, patched_connector):
        ctx = _ctx(tmp_path)
        ctx.gmail_authenticator._test_results = [
            _fake_email_message(message_id="m1"),
            _fake_email_message(message_id="m2", from_email="bob@example.com"),
        ]
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "search_gmail").invoke({
            "query": "alex", "max_results": 5,
        })
        assert out["query_used"] == "alex"
        assert len(out["candidates"]) == 2

    def test_caps_max_results(self, tmp_path, patched_connector):
        ctx = _ctx(tmp_path)
        ctx.gmail_authenticator._test_results = []
        tools = build_tools(ctx)
        # 50 should fail validation (le=10).
        with pytest.raises(Exception):
            _tool_by_name(tools, "search_gmail").invoke({
                "query": "x", "max_results": 50,
            })

    def test_blank_query_returns_empty(self, tmp_path, patched_connector):
        ctx = _ctx(tmp_path)
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "search_gmail").invoke({"query": "   "})
        assert out["candidates"] == []


# ─── read_thread ──────────────────────────────────────────────────────


class TestReadThread:
    def test_identifies_first_external_sender(
        self, tmp_path, patched_connector, monkeypatch,
    ):
        # Build a thread: first internal (us), then external (Carol
        # from third-party), then internal again (Bob's reply). The
        # tool must identify Carol's message as the first external.
        monkeypatch.setenv("SAI_INTERNAL_DOMAINS", "ourcorp.example")
        ctx = _ctx(tmp_path)
        ctx.gmail_authenticator._test_thread = [
            _fake_email_message(
                message_id="m1aaaabbbbccccdddd",
                from_email="alice@ourcorp.example",
                received_at=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
            ),
            _fake_email_message(
                message_id="m2aaaabbbbccccdddd",
                from_email="carol@external.example",
                from_name="Carol Carter",
                received_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
            ),
            _fake_email_message(
                message_id="m3aaaabbbbccccdddd",
                from_email="bob@ourcorp.example",
                received_at=datetime(2026, 4, 3, 11, 0, tzinfo=UTC),
            ),
        ]
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "read_thread").invoke({
            "thread_id": "thread123aaabbbcccddd",
        })
        assert out["first_external_index"] == 1
        assert out["first_external_message_id"] == "m2aaaabbbbccccdddd"
        assert out["first_external_from_email"] == "carol@external.example"
        assert out["internal_domains"] == ["ourcorp.example"]

    def test_handles_only_internal_thread(
        self, tmp_path, patched_connector, monkeypatch,
    ):
        monkeypatch.setenv("SAI_INTERNAL_DOMAINS", "ourcorp.example")
        ctx = _ctx(tmp_path)
        ctx.gmail_authenticator._test_thread = [
            _fake_email_message(from_email="alice@ourcorp.example"),
            _fake_email_message(from_email="bob@ourcorp.example"),
        ]
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "read_thread").invoke({
            "thread_id": "thread123aaabbbcccddd",
        })
        assert out["first_external_index"] is None
        assert out["first_external_message_id"] is None

    def test_multiple_internal_domains(
        self, tmp_path, patched_connector, monkeypatch,
    ):
        monkeypatch.setenv(
            "SAI_INTERNAL_DOMAINS", "ourcorp.example,sister.example",
        )
        ctx = _ctx(tmp_path)
        ctx.gmail_authenticator._test_thread = [
            _fake_email_message(from_email="x@sister.example"),
            _fake_email_message(from_email="x@ourcorp.example"),
            _fake_email_message(from_email="x@external.example"),
        ]
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "read_thread").invoke({
            "thread_id": "thread123aaabbbcccddd",
        })
        assert out["first_external_index"] == 2
        assert sorted(out["internal_domains"]) == [
            "ourcorp.example", "sister.example",
        ]

    def test_invalid_thread_id_returns_error(self, tmp_path):
        ctx = _ctx(tmp_path)
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "read_thread").invoke({
            "thread_id": "bad!chars",
        })
        assert "error" in out


class TestIsExternalSenderHelper:
    def test_internal(self):
        assert not _is_external_sender("alice@ourcorp.example", {"ourcorp.example"})

    def test_external(self):
        assert _is_external_sender("carol@external.example", {"ourcorp.example"})

    def test_with_display_name(self):
        assert _is_external_sender(
            "Carol Carter <carol@external.example>", {"ourcorp.example"},
        )

    def test_no_at_treated_as_external(self):
        assert _is_external_sender("weird-no-at-shape", {"ourcorp.example"})


# ─── list_gmail_labels ────────────────────────────────────────────────


class TestListGmailLabels:
    def test_returns_any_label_not_just_l1(self, tmp_path):
        ctx = _ctx(tmp_path, gmail_labels=[
            "L1/Customers", "L1/Keynote", "Important", "Receipts/2024",
            "L2/Action",
            "INBOX", "SENT", "CHAT",  # system labels — filtered out
        ])
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "list_gmail_labels").invoke({})
        # All non-system labels visible.
        assert "L1/Customers" in out["labels"]
        assert "L1/Keynote" in out["labels"]
        assert "Important" in out["labels"]
        assert "Receipts/2024" in out["labels"]
        assert "L2/Action" in out["labels"]
        # System labels filtered.
        assert "INBOX" not in out["labels"]
        assert "SENT" not in out["labels"]
        assert "CHAT" not in out["labels"]

    def test_contains_filter(self, tmp_path):
        ctx = _ctx(tmp_path, gmail_labels=[
            "L1/Customers", "L1/Keynote", "Important",
        ])
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "list_gmail_labels").invoke({
            "contains": "keynote",
        })
        assert out["labels"] == ["L1/Keynote"]

    def test_uses_cache(self, tmp_path):
        ctx = _ctx(tmp_path, gmail_labels=["L1/Customers"])
        tools = build_tools(ctx)
        tool = _tool_by_name(tools, "list_gmail_labels")
        tool.invoke({})
        # Tamper so a second API call would error.
        ctx.gmail_authenticator.build_service.side_effect = RuntimeError("boom")
        out = tool.invoke({})
        assert "L1/Customers" in out["labels"]


# ─── propose_classifier_rule (writes to rules tier) ───────────────────


class TestProposeClassifierRule:
    def test_refuses_label_not_in_gmail(self, tmp_path):
        ctx = _ctx(tmp_path, gmail_labels=["L1/Customers"])
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "propose_classifier_rule").invoke({
            "target": "alex@example.com",
            "target_kind": "sender_email",
            "label": "L1/Keynote",  # not in labels
            "why": "x",
        })
        assert "error" in out
        assert "doesn't exist" in out["error"]

    def test_stages_yaml_with_target_system_marker(self, tmp_path):
        ctx = _ctx(tmp_path, gmail_labels=["L1/Customers", "L1/Partners"])
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "propose_classifier_rule").invoke({
            "target": "external.example",
            "target_kind": "sender_domain",
            "label": "L1/Partners",
            "why": "all external.example mail belongs to Partners",
        })
        assert "error" not in out
        assert out["proposal_id"].startswith("rule_add::")
        loaded = yaml.safe_load(Path(out["staged_path"]).read_text())
        assert loaded["kind"] == "rule_add"
        assert loaded["target_system"] == "classifier_rules"
        assert loaded["eval_dataset_to_update"] == "canaries"
        assert loaded["gmail_label"] == "L1/Partners"
        assert loaded["expected_level1_classification"] == "partners"
        assert "✅" not in out["operator_message"]
        assert "white_check_mark" in out["operator_message"]

    def test_invalid_target_kind_rejected(self, tmp_path):
        ctx = _ctx(tmp_path, gmail_labels=["L1/Customers"])
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "propose_classifier_rule").invoke({
            "target": "alex@example.com",
            "target_kind": "weird",
            "label": "L1/Customers",
            "why": "x",
        })
        assert "error" in out

    def test_sender_email_without_at_rejected(self, tmp_path):
        ctx = _ctx(tmp_path, gmail_labels=["L1/Customers"])
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "propose_classifier_rule").invoke({
            "target": "alex.com",
            "target_kind": "sender_email",
            "label": "L1/Customers",
            "why": "x",
        })
        assert "error" in out


# ─── propose_llm_example (writes to LLM tier) ─────────────────────────


class TestProposeLlmExample:
    def test_stages_yaml_with_resolved_fields(self, tmp_path, patched_connector):
        ctx = _ctx(tmp_path, gmail_labels=["L1/Partners"])
        ctx.gmail_authenticator._test_results = [
            _fake_email_message(
                message_id="abcdefghij1234567890",
                from_email="carol@external.example",
                from_name="Carol Carter",
                subject="Q3 rollout",
            ),
        ]
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "propose_llm_example").invoke({
            "message_id": "abcdefghij1234567890",
            "label": "L1/Partners",
            "why": "first external sender, partner thread",
        })
        assert "error" not in out
        loaded = yaml.safe_load(Path(out["staged_path"]).read_text())
        assert loaded["kind"] == "eval_add"
        assert loaded["target_system"] == "llm_classifier"
        assert loaded["eval_dataset_to_update"] == "edge_cases"
        assert loaded["resolved_message_id"] == "abcdefghij1234567890"
        assert loaded["resolved_from_email"] == "carol@external.example"
        assert loaded["gmail_label"] == "L1/Partners"

    def test_refuses_label_not_in_gmail(self, tmp_path, patched_connector):
        ctx = _ctx(tmp_path, gmail_labels=["L1/Customers"])
        tools = build_tools(ctx)
        out = _tool_by_name(tools, "propose_llm_example").invoke({
            "message_id": "abcdefghij1234567890",
            "label": "L1/Partners",
            "why": "x",
        })
        assert "error" in out


# ─── registry shape ───────────────────────────────────────────────────


class TestRegistry:
    def test_six_tools(self):
        names = [s.name for s in REGISTERED_TOOL_SPECS]
        assert sorted(names) == sorted([
            "search_gmail", "read_message", "read_thread",
            "list_gmail_labels",
            "propose_classifier_rule", "propose_llm_example",
        ])

    def test_rights_only_two_tiers(self):
        for s in REGISTERED_TOOL_SPECS:
            assert s.rights in ("read_only", "propose_only")

    def test_propose_tools_split_by_target(self):
        propose = [s for s in REGISTERED_TOOL_SPECS if s.rights == "propose_only"]
        names = {s.name for s in propose}
        assert names == {"propose_classifier_rule", "propose_llm_example"}

    def test_bucket_pattern_accepts_any_label_shape(self):
        for ok in [
            "Customers", "L1/Keynote", "Receipts/2024", "Important",
            "Has Spaces", "kebab-case", "L2/Action_Required",
        ]:
            assert BUCKET_NAME_PATTERN.match(ok), f"should accept: {ok!r}"
        for bad in ["", "x" * 100, "weird@chars!"]:
            assert not BUCKET_NAME_PATTERN.match(bad), f"should reject: {bad!r}"


# ─── env config ───────────────────────────────────────────────────────


class TestInternalDomainsConfig:
    def test_default_is_placeholder(self, monkeypatch):
        # The PUBLIC default is example.com per principle #17 — operator
        # must override in their private overlay. A real domain here
        # would leak operator identity into PUBLIC code.
        monkeypatch.delenv("SAI_INTERNAL_DOMAINS", raising=False)
        assert _internal_domains() == {"example.com"}

    def test_comma_separated(self, monkeypatch):
        monkeypatch.setenv("SAI_INTERNAL_DOMAINS", "a.com, b.com ,  c.com")
        assert _internal_domains() == {"a.com", "b.com", "c.com"}

    def test_empty_falls_back_to_placeholder(self, monkeypatch):
        monkeypatch.setenv("SAI_INTERNAL_DOMAINS", "")
        assert _internal_domains() == {"example.com"}
