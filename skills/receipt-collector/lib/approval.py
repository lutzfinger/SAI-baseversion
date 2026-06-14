"""
approval — durable, surface-aware operator-approval primitive.

After the runner builds a review artifact (final-review.md), it must
pause until the operator approves. This module persists the request
as a row on disk (per SAI #3 "approval as durable state") so the
process can crash and restart without losing the gate.

Surfaces:
  * cli     — interactive prompt at stdin (default)
  * slack   — post a message to a channel and wait for reaction (Phase C)
  * email   — send a confirmation email and wait for reply (Phase C)
  * file    — write a sentinel file the operator touches to approve

For Phase B (this module's first cut) only the `cli` and `file`
surfaces are wired. Slack/email are declared as `kind:` values so the
state file's shape doesn't change in Phase C; only the *worker* that
polls the surface needs to be added.

Public API:
    open_request(trip_slug, surface, prompt_text,
                 state_root=...) -> ApprovalRequest
    poll(request) -> ApprovalState  # one of OPEN/APPROVED/REJECTED/EXPIRED
    await_approval(request, timeout_seconds, poll_interval=...) -> ApprovalState
    close(request, state)   # marks the row resolved

State file layout (one JSONL row per state transition):
    ~/Library/Application Support/SAI/receipt-collector/approvals/<request_id>.jsonl

Each row:
    {"ts": <unix_ts>, "state": "OPEN"|"APPROVED"|...,
     "surface": "cli"|"slack"|...,
     "trip_slug": "...", "actor": "operator"|"system", "note": "..."}

Per SAI #6a (schema enforcement at every boundary), reply parsing
uses canonical APPROVE_TOKENS / REJECT_TOKENS; anything else is
"feedback" and keeps the request OPEN (per #16g pending intents).
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class ApprovalState(str, Enum):
    OPEN = "OPEN"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    DROPPED = "DROPPED"


# Canonical reply tokens per SAI principle #6a. The parser matches
# tokens case-insensitively; surrounding punctuation/whitespace is
# stripped. Anything not in either list is "feedback" — the request
# stays OPEN and the runner re-asks rather than guessing intent.
APPROVE_TOKENS = {
    "approve", "approved", "yes", "y", "yep", "yup", "k", "kk",
    "ok", "okay", "lgtm", "sg", "sounds good", "looks good",
    "go", "ship it", "do it", "✅", "👍",
}
REJECT_TOKENS = {
    "reject", "rejected", "no", "n", "stop", "cancel", "abort",
    "drop", "no thanks", "❌", "👎",
}


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    trip_slug: str
    surface: str
    prompt_text: str
    state_path: Path
    created_ts: int = field(default_factory=lambda: int(time.time()))


def _state_root() -> Path:
    return Path(os.path.expanduser(
        "~/Library/Application Support/SAI/receipt-collector/approvals"
    ))


def open_request(
    trip_slug: str,
    surface: str,
    prompt_text: str,
    state_root: Optional[Path] = None,
) -> ApprovalRequest:
    """Open a new approval request. Persists state to disk."""
    root = Path(state_root) if state_root else _state_root()
    root.mkdir(parents=True, exist_ok=True)
    req_id = f"{trip_slug}-{uuid.uuid4().hex[:8]}"
    state_path = root / f"{req_id}.jsonl"
    req = ApprovalRequest(
        request_id=req_id,
        trip_slug=trip_slug,
        surface=surface,
        prompt_text=prompt_text,
        state_path=state_path,
    )
    _append(state_path, {
        "state": ApprovalState.OPEN.value,
        "surface": surface,
        "trip_slug": trip_slug,
        "actor": "system",
        "prompt": prompt_text,
    })
    return req


def _append(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": int(time.time()), **payload}
    with path.open("a") as f:
        f.write(json.dumps(payload) + "\n")


def _last_state(path: Path) -> ApprovalState:
    if not path.exists():
        return ApprovalState.EXPIRED
    last = ApprovalState.OPEN
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        s = row.get("state")
        if s in {v.value for v in ApprovalState}:
            last = ApprovalState(s)
    return last


def classify_reply(text: str) -> Optional[ApprovalState]:
    """Map a free-form reply to APPROVED/REJECTED, else None ("feedback").

    Match strategy (most-specific first):
      1. Full-text exact match against multi-word tokens
         (e.g., "looks good" — the operator wrote exactly that).
      2. Substring match against multi-word tokens
         (e.g., "looks good to me" contains "looks good").
      3. Whole-word match against single-word tokens
         (e.g., "yes!" → "yes").
    Anything else is None ("feedback").
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    t = t.strip(".,!?;:'\"`")
    # Exact full-text match (single OR multi-word tokens).
    if t in APPROVE_TOKENS:
        return ApprovalState.APPROVED
    if t in REJECT_TOKENS:
        return ApprovalState.REJECTED
    # Multi-word substring match (so "looks good to me" → APPROVED via "looks good").
    for tok in APPROVE_TOKENS:
        if " " in tok and tok in t:
            return ApprovalState.APPROVED
    for tok in REJECT_TOKENS:
        if " " in tok and tok in t:
            return ApprovalState.REJECTED
    # Single-word match across the split.
    words = t.split()
    if any(w in APPROVE_TOKENS for w in words):
        return ApprovalState.APPROVED
    if any(w in REJECT_TOKENS for w in words):
        return ApprovalState.REJECTED
    return None


def poll(request: ApprovalRequest) -> ApprovalState:
    """Return the current state without blocking."""
    return _last_state(request.state_path)


def record_reply(
    request: ApprovalRequest,
    reply_text: str,
    actor: str = "operator",
) -> ApprovalState:
    """Parse a reply and append a state transition. Returns the new state.

    If the reply parses to APPROVED/REJECTED, that's a terminal state.
    Otherwise (feedback / unrecognised) the request stays OPEN; a
    feedback row is appended so the audit log shows the operator was
    heard, per principle #30 (confirmation + clarification).
    """
    parsed = classify_reply(reply_text)
    if parsed in (ApprovalState.APPROVED, ApprovalState.REJECTED):
        _append(request.state_path, {
            "state": parsed.value,
            "surface": request.surface,
            "trip_slug": request.trip_slug,
            "actor": actor,
            "note": reply_text.strip()[:160],
        })
        return parsed
    # Feedback row.
    _append(request.state_path, {
        "state": ApprovalState.OPEN.value,
        "surface": request.surface,
        "trip_slug": request.trip_slug,
        "actor": actor,
        "feedback": reply_text.strip()[:240],
    })
    return ApprovalState.OPEN


def await_approval_cli(
    request: ApprovalRequest,
    timeout_seconds: int | None = None,
) -> ApprovalState:
    """Block on stdin until the operator approves/rejects/aborts.

    For non-Slack/email surfaces, this is the immediate fallback. For
    Slack/email, the Phase C worker polls the channel/inbox and calls
    `record_reply` for each operator message; the runner calls
    `poll(request)` in a loop.
    """
    print()
    print("=" * 72)
    print(request.prompt_text)
    print("=" * 72)
    print(f"Approve?  (one of: {', '.join(sorted(APPROVE_TOKENS - {'✅', '👍'}))})")
    print(f"Reject?   (one of: {', '.join(sorted(REJECT_TOKENS - {'❌', '👎'}))})")
    print()
    deadline = (time.time() + timeout_seconds) if timeout_seconds else None
    try:
        while True:
            if deadline and time.time() > deadline:
                _append(request.state_path, {
                    "state": ApprovalState.EXPIRED.value,
                    "surface": request.surface,
                    "trip_slug": request.trip_slug,
                    "actor": "system",
                    "note": "timeout",
                })
                return ApprovalState.EXPIRED
            try:
                text = input("> ").strip()
            except EOFError:
                _append(request.state_path, {
                    "state": ApprovalState.DROPPED.value,
                    "surface": request.surface,
                    "trip_slug": request.trip_slug,
                    "actor": "operator",
                    "note": "EOF",
                })
                return ApprovalState.DROPPED
            state = record_reply(request, text)
            if state is ApprovalState.APPROVED:
                print("Approved.")
                return ApprovalState.APPROVED
            if state is ApprovalState.REJECTED:
                print("Rejected.")
                return ApprovalState.REJECTED
            # Feedback — re-prompt.
            print(f"(Recorded as feedback. The request stays open. "
                  f"Type 'yes' to approve or 'no' to reject.)")
    except KeyboardInterrupt:
        _append(request.state_path, {
            "state": ApprovalState.DROPPED.value,
            "surface": request.surface,
            "trip_slug": request.trip_slug,
            "actor": "operator",
            "note": "Ctrl-C",
        })
        print()
        return ApprovalState.DROPPED


def await_approval_file(
    request: ApprovalRequest,
    sentinel_dir: Path,
    timeout_seconds: int | None = None,
    poll_interval: float = 1.5,
) -> ApprovalState:
    """Block until the operator touches a sentinel file.

    The sentinel file's name encodes the verdict:
      <sentinel_dir>/<request_id>.approve   → APPROVED
      <sentinel_dir>/<request_id>.reject    → REJECTED
      <sentinel_dir>/<request_id>.drop      → DROPPED

    Useful for headless / scripted approval (e.g. a co-work session
    where the operator clicks a button in another window).
    """
    sentinel_dir = Path(sentinel_dir)
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "approve": sentinel_dir / f"{request.request_id}.approve",
        "reject": sentinel_dir / f"{request.request_id}.reject",
        "drop": sentinel_dir / f"{request.request_id}.drop",
    }
    deadline = (time.time() + timeout_seconds) if timeout_seconds else None
    while True:
        for verdict, p in paths.items():
            if p.exists():
                state = {
                    "approve": ApprovalState.APPROVED,
                    "reject": ApprovalState.REJECTED,
                    "drop": ApprovalState.DROPPED,
                }[verdict]
                _append(request.state_path, {
                    "state": state.value,
                    "surface": "file",
                    "trip_slug": request.trip_slug,
                    "actor": "operator",
                    "note": f"sentinel:{p.name}",
                })
                return state
        if deadline and time.time() > deadline:
            _append(request.state_path, {
                "state": ApprovalState.EXPIRED.value,
                "surface": "file",
                "trip_slug": request.trip_slug,
                "actor": "system",
                "note": "timeout",
            })
            return ApprovalState.EXPIRED
        time.sleep(poll_interval)


def history(request: ApprovalRequest) -> list[dict]:
    if not request.state_path.exists():
        return []
    return [
        json.loads(ln)
        for ln in request.state_path.read_text().splitlines()
        if ln.strip()
    ]
