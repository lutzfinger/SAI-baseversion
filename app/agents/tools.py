"""Tool surface for the sai-eval agent — LangChain edition.

Each tool is a LangChain ``@tool``-decorated closure built per-invocation
so it can capture the operator's identity, the source text, the proposed
directory, and the Gmail authenticator without LangChain magic.

Two rights tiers (still enforced by which functions exist + what they do):

  * **read_only** — Gmail read API only; no mutation
  * **propose_only** — writes a YAML proposal under ``eval/proposed/``;
    NEVER mutates rules or eval datasets directly

Key design decisions per operator feedback (2026-05-02 evening):

  * Labels: returns ANY Gmail label, not L1/* only. The operator picks
    the convention; the agent reads what's actually there.
  * Propose tools split by **target system** so the agent reasons
    about WHERE the change goes:
      - ``propose_classifier_rule`` → keyword-classify.md (rules tier)
      - ``propose_llm_example``     → edge_cases.jsonl (LLM tier)
    Apply paths route the eval-row regen accordingly.
  * ``read_thread`` exposes the message chain so the agent can walk
    back to the **first external sender** before proposing — matches
    the operator's classifier convention (L1 keyed off first sender
    not in internal_domains, env var SAI_INTERNAL_DOMAINS).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

LOGGER = logging.getLogger(__name__)

# Hard caps enforced regardless of LLM-supplied args.
MAX_QUERY_LENGTH: int = 256
MAX_SEARCH_RESULTS: int = 10
MAX_SNIPPET_CHARS: int = 200
MAX_BODY_EXCERPT_CHARS: int = 2000
MAX_THREAD_MESSAGES: int = 25
MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,200}$")
THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,200}$")

# Bucket name pattern — accepts ANY Gmail label name, not gated on L1/.
# We strip whitespace, keep alphanumerics + a few separators.
BUCKET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_/\-\. ]{1,64}$")

# Operator's internal domains — for the first-external-sender rule.
# Configurable via env so e1, e2, future workflows can override.
def _internal_domains() -> set[str]:
    """Operator's internal email domains. Default ``example.com`` is a
    placeholder — operator MUST set ``SAI_INTERNAL_DOMAINS`` in their
    private overlay's runtime.env so the first-external-sender rule
    works against their real domain. Keeping a real domain here would
    leak the operator's identity into PUBLIC code (principle #17)."""

    raw = os.environ.get("SAI_INTERNAL_DOMAINS", "").strip()
    if not raw:
        raw = "example.com"
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


# ─── per-invocation context (captured into tool closures) ─────────────


@dataclass
class ToolContext:
    """Per-invocation state passed to every tool via closure capture."""

    proposed_by: str
    source_text: str
    proposed_dir: Path
    gmail_authenticator: Any
    cache: dict[str, Any]


# ─── shared helpers ───────────────────────────────────────────────────


def _truncate_snippet(s: str | None) -> str:
    return (s or "")[:MAX_SNIPPET_CHARS]


def _truncate_body(s: str | None) -> str:
    return (s or "")[:MAX_BODY_EXCERPT_CHARS]


def _stage_proposal_yaml(
    *, proposed_dir: Path, proposal_id: str, payload: dict[str, Any],
) -> Path:
    import yaml

    proposed_dir.mkdir(parents=True, exist_ok=True)
    safe_id = proposal_id.replace("::", "__").replace("/", "_")
    out = proposed_dir / f"{safe_id}.yaml"
    out.write_text(
        yaml.safe_dump(payload, sort_keys=True, default_flow_style=False),
        encoding="utf-8",
    )
    return out


def _proposal_id(kind: str, slug: str) -> str:
    safe_slug = re.sub(r"[^a-zA-Z0-9._-]", "_", slug)[:60]
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}::{ts}::{safe_slug}"


def _is_external_sender(from_email: str, internal_domains: set[str]) -> bool:
    """True if the email's domain is NOT in internal_domains.

    Matches email shape `local@domain` or `Name <local@domain>`. If
    parsing fails we treat as external (safer — agent will surface).
    """

    addr = from_email.strip()
    if "<" in addr:
        addr = addr.split("<", 1)[1].split(">", 1)[0]
    if "@" not in addr:
        return True
    domain = addr.split("@", 1)[1].strip().lower()
    return domain not in internal_domains


# ─── tool input schemas (Pydantic — used for LangChain StructuredTool
# validation AND for tests) ───────────────────────────────────────────


class SearchGmailInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ..., max_length=MAX_QUERY_LENGTH,
        description=(
            "Gmail search query in Gmail's normal syntax. Examples: "
            "`from:alex@example.com`, `subject:Q3 numbers`, "
            "`subject:\"acme rollout\"`, `newer_than:7d from:example.org`."
        ),
    )
    max_results: int = Field(
        5, ge=1, le=MAX_SEARCH_RESULTS,
        description=f"Max results (1-{MAX_SEARCH_RESULTS}). Default 5.",
    )


class ReadMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(
        ..., description="Gmail message id from a prior search_gmail result.",
    )


class ReadThreadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(
        ..., description=(
            "Gmail thread id (from search_gmail or read_message output). "
            "Returns up to "
            f"{MAX_THREAD_MESSAGES} messages in the thread, oldest first, "
            "with sender + subject + snippet for each — and a flag for "
            "which one is the FIRST EXTERNAL message (first sender outside "
            "the operator's own domains)."
        ),
    )


class ListGmailLabelsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contains: str | None = Field(
        None, max_length=64,
        description=(
            "Optional case-insensitive substring filter — e.g. 'L1' to "
            "see only L1/* labels, or 'keynote' to check if a specific "
            "label exists. Omit to list everything."
        ),
    )


class ProposeClassifierRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(
        ..., min_length=3, max_length=200,
        description=(
            "Sender email (alex@example.com) or domain (example.com) the "
            "rule should match. For threaded conversations remember the "
            "operator's convention: classify off the FIRST EXTERNAL sender."
        ),
    )
    target_kind: str = Field(
        ..., description="`sender_email` (has @) or `sender_domain`.",
    )
    label: str = Field(
        ..., description=(
            "Gmail label name to apply (any label, e.g. `L1/Customers`, "
            "`Receipts`, `L1/Partners`). Must already exist in Gmail; "
            "verify with list_gmail_labels first."
        ),
    )
    why: str = Field(
        ..., max_length=500, description="One-sentence rationale (audit).",
    )


class ProposeLlmExampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(
        ..., description=(
            "Gmail message id (must come from search_gmail / read_message "
            "/ read_thread — the agent should not invent ids). For thread "
            "context use read_thread, then pick the FIRST EXTERNAL message."
        ),
    )
    label: str = Field(
        ..., description="Gmail label (must already exist; verify first).",
    )
    why: str = Field(
        ..., max_length=500, description="Why this email is the right teaching example.",
    )


# ─── tool builders (closures over ToolContext) ────────────────────────


def build_tools(ctx: ToolContext) -> list[Any]:
    """Build the LangChain StructuredTool list bound to a ToolContext.

    Called once per agent invocation. Each returned tool captures `ctx`
    in its closure so the LLM sees only the LLM-facing parameters.
    """

    return [
        _build_search_gmail(ctx),
        _build_read_message(ctx),
        _build_read_thread(ctx),
        _build_list_gmail_labels(ctx),
        _build_propose_classifier_rule(ctx),
        _build_propose_llm_example(ctx),
    ]


# ── search_gmail ──────────────────────────────────────────────────────


def _build_search_gmail(ctx: ToolContext) -> StructuredTool:
    def _search_gmail(query: str, max_results: int = 5) -> dict[str, Any]:
        from app.connectors.gmail import GmailAPIConnector

        q = (query or "").strip()
        if not q:
            return {"query_used": "", "candidates": []}

        connector = GmailAPIConnector(
            authenticator=ctx.gmail_authenticator,
            user_id="me",
            query=q,
            label_ids=[],
            max_results=min(max_results, MAX_SEARCH_RESULTS),
        )
        messages = connector.fetch_messages()
        return {
            "query_used": q,
            "candidates": [
                {
                    "message_id": m.message_id,
                    "thread_id": m.thread_id,
                    "from_email": m.from_email,
                    "from_name": m.from_name,
                    "subject": (m.subject or "(no subject)")[:200],
                    "snippet": _truncate_snippet(m.snippet),
                    "received_at_iso": (
                        m.received_at.isoformat() if m.received_at else None
                    ),
                }
                for m in messages
            ],
        }

    return StructuredTool.from_function(
        name="search_gmail",
        description=(
            "Search Gmail using normal Gmail query syntax. Returns up to "
            "max_results message summaries (no body). Use this FIRST to "
            "find the email the operator is referring to. For subject "
            "references use `subject:\"...\"`. For thread context after a "
            "hit, follow with read_thread."
        ),
        args_schema=SearchGmailInput,
        func=_search_gmail,
    )


# ── read_message ──────────────────────────────────────────────────────


def _build_read_message(ctx: ToolContext) -> StructuredTool:
    def _read_message(message_id: str) -> dict[str, Any]:
        if not MESSAGE_ID_PATTERN.match(message_id):
            return {"error": f"message_id has unexpected shape: {message_id!r}"}

        from app.connectors.gmail import GmailAPIConnector

        connector = GmailAPIConnector(
            authenticator=ctx.gmail_authenticator,
            user_id="me",
            query=f"rfc822msgid:{message_id}",
            label_ids=[],
            max_results=1,
        )
        messages = connector.fetch_messages()
        if not messages:
            service = ctx.gmail_authenticator.build_service()
            msg = (
                service.users().messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
            headers = {
                h.get("name", "").lower(): h.get("value", "")
                for h in msg.get("payload", {}).get("headers", [])
                if isinstance(h, dict)
            }
            return {
                "message_id": message_id,
                "thread_id": msg.get("threadId"),
                "from_email": headers.get("from", ""),
                "from_name": None,
                "to": [headers.get("to", "")] if headers.get("to") else [],
                "subject": headers.get("subject", "")[:200],
                "snippet": _truncate_snippet(msg.get("snippet")),
                "body_excerpt": "",
                "received_at_iso": None,
            }

        m = messages[0]
        return {
            "message_id": m.message_id,
            "thread_id": m.thread_id,
            "from_email": m.from_email,
            "from_name": m.from_name,
            "to": list(m.to or []),
            "subject": (m.subject or "")[:200],
            "snippet": _truncate_snippet(m.snippet),
            "body_excerpt": _truncate_body(m.body_excerpt or m.snippet),
            "received_at_iso": (
                m.received_at.isoformat() if m.received_at else None
            ),
        }

    return StructuredTool.from_function(
        name="read_message",
        description=(
            "Fetch the snippet + body excerpt for ONE Gmail message by id. "
            "Use after search_gmail when you need more context for a single "
            "message."
        ),
        args_schema=ReadMessageInput,
        func=_read_message,
    )


# ── read_thread (the new piece for first-external-sender) ─────────────


def _build_read_thread(ctx: ToolContext) -> StructuredTool:
    def _read_thread(thread_id: str) -> dict[str, Any]:
        if not THREAD_ID_PATTERN.match(thread_id):
            return {"error": f"thread_id has unexpected shape: {thread_id!r}"}

        from app.connectors.gmail import GmailAPIConnector

        connector = GmailAPIConnector(
            authenticator=ctx.gmail_authenticator,
            user_id="me",
            query="",  # not used by fetch_thread_messages
            label_ids=[],
            max_results=MAX_THREAD_MESSAGES,
        )
        try:
            thread_msgs = connector.fetch_thread_messages(thread_id=thread_id)
        except Exception as exc:
            return {"error": f"thread fetch failed: {exc}"}

        thread_msgs = thread_msgs[:MAX_THREAD_MESSAGES]
        # Sort oldest first.
        thread_msgs.sort(
            key=lambda m: m.received_at or datetime.min.replace(tzinfo=UTC)
        )

        internal = _internal_domains()
        first_external_idx: int | None = None
        out_messages: list[dict[str, Any]] = []
        for idx, m in enumerate(thread_msgs):
            external = _is_external_sender(m.from_email, internal)
            if first_external_idx is None and external:
                first_external_idx = idx
            out_messages.append({
                "index": idx,
                "message_id": m.message_id,
                "from_email": m.from_email,
                "from_name": m.from_name,
                "subject": (m.subject or "")[:200],
                "snippet": _truncate_snippet(m.snippet),
                "received_at_iso": (
                    m.received_at.isoformat() if m.received_at else None
                ),
                "is_external": external,
            })

        return {
            "thread_id": thread_id,
            "internal_domains": sorted(internal),
            "messages": out_messages,
            "first_external_index": first_external_idx,
            "first_external_message_id": (
                out_messages[first_external_idx]["message_id"]
                if first_external_idx is not None else None
            ),
            "first_external_from_email": (
                out_messages[first_external_idx]["from_email"]
                if first_external_idx is not None else None
            ),
        }

    return StructuredTool.from_function(
        name="read_thread",
        description=(
            "Read all messages in a Gmail thread (oldest first). Returns "
            "each message's sender / subject / snippet AND identifies the "
            "FIRST EXTERNAL sender — the first message in the chain from a "
            "sender NOT in the operator's internal domains. The operator's "
            "classification convention is: L1 is keyed off the first "
            "external sender, NOT the latest reply. ALWAYS use this before "
            "proposing a classifier rule or LLM example based on a thread."
        ),
        args_schema=ReadThreadInput,
        func=_read_thread,
    )


# ── list_gmail_labels (any label, not L1/* only) ──────────────────────


def _build_list_gmail_labels(ctx: ToolContext) -> StructuredTool:
    def _list_labels(contains: str | None = None) -> dict[str, Any]:
        cache_key = f"labels::{contains or ''}"
        if cache_key in ctx.cache:
            return {"labels": list(ctx.cache[cache_key])}

        service = ctx.gmail_authenticator.build_service()
        response = service.users().labels().list(userId="me").execute()
        items = response.get("labels", []) or []
        all_names = sorted({
            str(item.get("name", ""))
            for item in items
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        })
        if contains:
            needle = contains.lower()
            filtered = [n for n in all_names if needle in n.lower()]
        else:
            filtered = all_names
        # Drop Gmail's built-in system labels (CHAT, SENT, etc.) which
        # the operator never tags with manually.
        system_prefixes = (
            "CHAT", "SENT", "INBOX", "IMPORTANT", "TRASH", "DRAFT",
            "SPAM", "STARRED", "UNREAD", "CATEGORY_",
        )
        filtered = [n for n in filtered if not n.startswith(system_prefixes)]
        ctx.cache[cache_key] = filtered
        return {"labels": filtered, "filter_used": contains}

    return StructuredTool.from_function(
        name="list_gmail_labels",
        description=(
            "List the operator's Gmail labels (any label, not just L1/*). "
            "Use this BEFORE proposing a classifier rule or LLM example to "
            "verify the label exists. The operator must create labels in "
            "Gmail themselves — you cannot create them. Use the optional "
            "`contains` filter to check a specific name (e.g. "
            "`contains=\"keynote\"`)."
        ),
        args_schema=ListGmailLabelsInput,
        func=_list_labels,
    )


# ── propose_classifier_rule (writes to the rules tier) ────────────────


def _build_propose_classifier_rule(ctx: ToolContext) -> StructuredTool:
    def _propose(
        target: str, target_kind: str, label: str, why: str,
    ) -> dict[str, Any]:
        if target_kind not in ("sender_email", "sender_domain"):
            return {"error": f"target_kind must be sender_email or sender_domain, got {target_kind!r}"}
        target = target.strip().lower()
        if target_kind == "sender_email" and "@" not in target:
            return {"error": f"target_kind=sender_email but {target!r} has no '@'"}
        if not BUCKET_NAME_PATTERN.match(label):
            return {"error": f"label {label!r} has unexpected shape"}

        # Verify the label actually exists in Gmail.
        labels_check = ctx.cache.get("labels_full") or _list_all_labels(ctx)
        if label not in labels_check:
            return {
                "error": (
                    f"Label `{label}` doesn't exist in Gmail yet. "
                    f"Available labels include: {', '.join(labels_check[:15])}"
                    + (" …" if len(labels_check) > 15 else "")
                    + ". Tell the operator to create the label in Gmail first."
                ),
            }

        proposal_id = _proposal_id("rule_add", target)
        # Strip a trailing L1/, L2/ prefix from the bucket-as-stored — the
        # rules tier uses the bucket name without prefix, the canary regen
        # adds back the L1/ display name from LEVEL1_DISPLAY_NAMES.
        bucket = label.split("/", 1)[1].lower() if "/" in label else label.lower()
        bucket = re.sub(r"[^a-z0-9_]", "_", bucket)
        payload = {
            "kind": "rule_add",
            "proposal_id": proposal_id,
            "proposed_at": datetime.now(UTC).isoformat(),
            "proposed_by": ctx.proposed_by,
            "target": target,
            "target_kind": target_kind,
            "expected_level1_classification": bucket,
            "gmail_label": label,
            "source_text": ctx.source_text,
            "agent_rationale": why,
            "target_system": "classifier_rules",
            "eval_dataset_to_update": "canaries",
        }
        path = _stage_proposal_yaml(
            proposed_dir=ctx.proposed_dir,
            proposal_id=proposal_id, payload=payload,
        )
        return {
            "proposal_id": proposal_id,
            "staged_path": str(path),
            "operator_message": (
                f"Proposed *classifier rule*: any email from `{target}` "
                f"→ `{label}`.\n_Reason:_ {why}\n"
                f"React :white_check_mark: to apply (canary will regenerate "
                f"+ regression will run before commit) or :x: to cancel."
            ),
        }

    return StructuredTool.from_function(
        name="propose_classifier_rule",
        description=(
            "Stage a proposal to add a sender→label classifier rule. This "
            "writes to the RULES tier (keyword-classify.md) and on apply "
            "will regenerate canaries.jsonl so the new rule has its own "
            "regression test. Use this when the operator wants a STANDING "
            "RULE (\"all emails from example.org should be L1/Partners\"), "
            "NOT when they want to teach the LLM about one specific email."
        ),
        args_schema=ProposeClassifierRuleInput,
        func=_propose,
    )


# ── propose_llm_example (writes to the LLM tier) ──────────────────────


def _build_propose_llm_example(ctx: ToolContext) -> StructuredTool:
    def _propose(message_id: str, label: str, why: str) -> dict[str, Any]:
        if not MESSAGE_ID_PATTERN.match(message_id):
            return {"error": f"message_id {message_id!r} has unexpected shape"}
        if not BUCKET_NAME_PATTERN.match(label):
            return {"error": f"label {label!r} has unexpected shape"}

        labels_check = ctx.cache.get("labels_full") or _list_all_labels(ctx)
        if label not in labels_check:
            return {
                "error": (
                    f"Label `{label}` doesn't exist in Gmail yet. "
                    f"Tell the operator to create it first."
                ),
            }

        # Pull the message details so the proposal carries everything
        # the apply path needs without a second Gmail fetch.
        # Fix 2026-05-04: rfc822msgid: only matches the RFC822
        # Message-ID HEADER, not Gmail's internal hex id (which is
        # what search_gmail returns). When rfc822msgid: gets 0 hits,
        # fall back to messages.get(id=...) using the Gmail API
        # internal-id endpoint — same pattern as read_message.
        from app.connectors.gmail import GmailAPIConnector

        connector = GmailAPIConnector(
            authenticator=ctx.gmail_authenticator,
            user_id="me",
            query=f"rfc822msgid:{message_id}",
            label_ids=[],
            max_results=1,
        )
        msgs = connector.fetch_messages()
        m: Any = None
        if msgs:
            m = msgs[0]
        else:
            # Fallback: Gmail internal ID lookup (same pattern as read_message).
            try:
                service = ctx.gmail_authenticator.build_service()
                msg = (
                    service.users().messages()
                    .get(userId="me", id=message_id, format="full")
                    .execute()
                )
                headers = {
                    h.get("name", "").lower(): h.get("value", "")
                    for h in msg.get("payload", {}).get("headers", [])
                    if isinstance(h, dict)
                }
                from types import SimpleNamespace
                m = SimpleNamespace(
                    message_id=message_id,
                    thread_id=msg.get("threadId"),
                    from_email=headers.get("from", ""),
                    from_name=None,
                    subject=headers.get("subject", "")[:200],
                    snippet=msg.get("snippet"),
                    received_at=None,
                )
            except Exception as exc:
                return {
                    "error": (
                        f"couldn't fetch message {message_id!r} via either "
                        f"rfc822msgid: search OR Gmail internal-id lookup: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                }

        proposal_id = _proposal_id("eval_add", message_id)
        bucket = label.split("/", 1)[1].lower() if "/" in label else label.lower()
        bucket = re.sub(r"[^a-z0-9_]", "_", bucket)
        payload = {
            "kind": "eval_add",
            "proposal_id": proposal_id,
            "proposed_at": datetime.now(UTC).isoformat(),
            "proposed_by": ctx.proposed_by,
            "message_ref": ctx.source_text,
            "expected_level1_classification": bucket,
            "gmail_label": label,
            "source_text": ctx.source_text,
            "agent_rationale": why,
            "target_system": "llm_classifier",
            "eval_dataset_to_update": "edge_cases",
            "resolved_message_id": m.message_id,
            "resolved_thread_id": m.thread_id,
            "resolved_from_email": m.from_email,
            "resolved_from_name": m.from_name,
            "resolved_subject": m.subject,
            "resolved_snippet": _truncate_snippet(m.snippet),
            "resolved_received_at_iso": (
                m.received_at.isoformat() if m.received_at else None
            ),
        }
        path = _stage_proposal_yaml(
            proposed_dir=ctx.proposed_dir,
            proposal_id=proposal_id, payload=payload,
        )
        return {
            "proposal_id": proposal_id,
            "staged_path": str(path),
            "operator_message": (
                f"Proposed *LLM teaching example*: this email → `{label}`.\n"
                f"> _{(m.subject or '(no subject)')[:80]}_ — "
                f"{m.from_name or m.from_email}\n_Reason:_ {why}\n"
                f"React :white_check_mark: to add to the eval set "
                f"(regression on edge_cases will run before commit) or "
                f":x: to cancel."
            ),
        }

    return StructuredTool.from_function(
        name="propose_llm_example",
        description=(
            "Stage a proposal to add ONE specific email to the LLM eval "
            "set as a teaching example. Writes to the LLM tier "
            "(edge_cases.jsonl). Use this when the operator wants the "
            "LLM to learn from a SPECIFIC email — NOT to set a standing "
            "rule. Pick the message_id carefully: for thread references "
            "use read_thread first and pick the FIRST EXTERNAL message."
        ),
        args_schema=ProposeLlmExampleInput,
        func=_propose,
    )


# ─── shared label-fetch helper (caches full label list across tools) ─


def _list_all_labels(ctx: ToolContext) -> list[str]:
    cached = ctx.cache.get("labels_full")
    if cached is not None:
        return list(cached)
    service = ctx.gmail_authenticator.build_service()
    response = service.users().labels().list(userId="me").execute()
    items = response.get("labels", []) or []
    system_prefixes = (
        "CHAT", "SENT", "INBOX", "IMPORTANT", "TRASH", "DRAFT",
        "SPAM", "STARRED", "UNREAD", "CATEGORY_",
    )
    names = sorted({
        str(item.get("name", ""))
        for item in items
        if isinstance(item, dict)
        and str(item.get("name", "")).strip()
        and not str(item.get("name", "")).startswith(system_prefixes)
    })
    ctx.cache["labels_full"] = names
    return names


# ─── tool surface metadata (for the surface YAML check + audit) ───────


@dataclass(frozen=True)
class ToolSpec:
    name: str
    rights: str  # "read_only" | "propose_only"
    one_liner: str


REGISTERED_TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("search_gmail",          "read_only",    "Gmail query → message summaries"),
    ToolSpec("read_message",          "read_only",    "One message snippet + body excerpt"),
    ToolSpec("read_thread",           "read_only",    "Whole thread + first-external flag"),
    ToolSpec("list_gmail_labels",     "read_only",    "All Gmail labels (any prefix)"),
    ToolSpec("propose_classifier_rule", "propose_only", "Stage classifier rule → canary regen on apply"),
    ToolSpec("propose_llm_example",   "propose_only", "Stage LLM teaching example → edge_cases regression on apply"),
]
