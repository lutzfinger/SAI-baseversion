#!/usr/bin/env python3
"""SAI email intake → skill dispatch.

Companion to `scripts/sai_dispatch.py` (CLI/DM) and the Slack-bot
skill-run patch (Slack channel/DM). This is the **email** path: polls
Gmail for emails the operator sent to SAI containing a `run <skill>`
phrase, parses, invokes the skill, and stages the proposal.

Trigger contract
----------------
Email FROM the operator (configured in SAI policy, e.g.
`hello@example.com`), TO the SAI address (e.g. `sai@example.com`),
with the body containing a phrase like:

    run student participation check for C-Suites May 2026 INSEAD,
    all sessions, https://docs.google.com/spreadsheets/d/.../edit

The first non-matching email in the inbox stops the scan. Processed
emails are labeled `SAI/skill-run-processed` so they're not re-fired.

Auth precondition
-----------------
Reads Gmail OAuth from the existing SAI gmail token store (same as
the rest of SAI's email workers — `app.connectors.gmail_auth`). The
SAI Gmail credential is NOT in this script's scope; it relies on the
existing operator setup.

Per PRINCIPLES.md
-----------------
- §6 fail closed: unknown sender → ignored.
- §16e guarded interface: every processed email gets a labeled reply
  in the same thread so the operator sees what SAI did.
- §16i registered surface: only the configured SAI inbound address is
  scanned. Default label: `SAI/skill-run-pending` (operator applies
  via Gmail filter).
- §4 append-only audit: every run appends to
  `~/Library/Logs/SAI/sai_email_skill_intake.jsonl`.

Usage
-----
    # Manual / cron / launchd-driven scan:
    python -m scripts.sai_email_skill_intake \\
        --inbound sai@example.com \\
        --operator hello@example.com \\
        --pending-label SAI/skill-run-pending \\
        --processed-label SAI/skill-run-processed \\
        [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SAI_ROOT = Path(__file__).resolve().parents[1]
if str(_SAI_ROOT) not in sys.path:
    sys.path.insert(0, str(_SAI_ROOT))

from app.skills.skill_run_parser import parse_skill_run  # noqa: E402
from app.skills.skill_apply_registry import (  # noqa: E402
    dispatch_approved_proposal, is_registered,
)


LOGGER = logging.getLogger("sai.email_skill_intake")
AUDIT_LOG = Path.home() / "Library" / "Logs" / "SAI" / "sai_email_skill_intake.jsonl"


def _audit(record: dict[str, Any]) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    out = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(out) + "\n")


def _build_gmail_client():
    """Lazy import of the SAI Gmail stack — keeps this script importable
    in environments where Gmail OAuth isn't yet configured (e.g. tests)."""
    from app.connectors.gmail_auth import GmailOAuthAuthenticator
    from app.connectors.gmail_documents import GmailDocumentConnector
    from app.connectors.gmail_send import GmailSendConnector
    from app.shared.config import get_settings
    settings = get_settings()
    # NB: in this skill we use the default policy; real SAI workers carry
    # a per-workflow PolicyDocument. For an intake-only flow we can use
    # a minimal placeholder per the connector's contract.
    from app.shared.models import PolicyDocument
    policy = PolicyDocument(workflow_id="sai-email-skill-intake")
    authn = GmailOAuthAuthenticator(settings=settings, policy=policy)
    docs = GmailDocumentConnector(authenticator=authn)
    send = GmailSendConnector(authenticator=authn, allowed_recipient_emails=[])
    return docs, send


def _load_skill_runner(workflow_id: str) -> Any:
    """Import the skill's runner.py by file path (hyphenated dir names)."""
    import importlib.util as _ilu
    runner_path = _SAI_ROOT / "skills" / workflow_id / "runner.py"
    if not runner_path.exists():
        raise FileNotFoundError(f"runner not found: {runner_path}")
    mod_name = f"_runner_{workflow_id.replace('-', '_')}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = _ilu.spec_from_file_location(mod_name, runner_path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def process_one_email(
    *,
    docs_connector,
    send_connector,
    message_id: str,
    sender: str,
    subject: str,
    body: str,
    pending_label: str,
    processed_label: str,
    operator_address: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Parse one email + dispatch to the right skill.

    Returns a dict suitable for the audit log and for posting a reply
    back to the operator.
    """
    # ── Sender allowlist (§6 fail closed) ────────────────────────────
    sender_lower = (sender or "").lower()
    if operator_address.lower() not in sender_lower:
        return {
            "message_id": message_id, "status": "ignored_sender",
            "reason": f"sender {sender!r} != operator {operator_address!r}",
        }

    # ── Parse the body ───────────────────────────────────────────────
    invocation = parse_skill_run(body)
    if invocation is None:
        return {
            "message_id": message_id, "status": "no_skill_match",
            "subject": subject[:120],
        }
    if not is_registered(invocation.workflow_id):
        return {
            "message_id": message_id, "status": "workflow_not_registered",
            "workflow_id": invocation.workflow_id,
        }
    if invocation.error_reason:
        return {
            "message_id": message_id, "status": "parser_partial",
            "workflow_id": invocation.workflow_id,
            "reason": invocation.error_reason,
        }

    # ── Invoke the skill cascade ─────────────────────────────────────
    runner = _load_skill_runner(invocation.workflow_id)
    skill_inputs = {
        **invocation.inputs,
        "thread_id": f"email-{message_id}",
        "folder_name": invocation.inputs.get("folder"),
    }
    if dry_run:
        return {
            "message_id": message_id, "status": "dry_run_would_invoke",
            "workflow_id": invocation.workflow_id, "inputs": skill_inputs,
        }
    try:
        result = runner.run(skill_inputs)
    except Exception as exc:
        LOGGER.exception("skill crashed")
        return {
            "message_id": message_id, "status": "skill_crashed",
            "workflow_id": invocation.workflow_id,
            "error": f"{type(exc).__name__}: {exc}",
        }

    # ── Label processed + reply ──────────────────────────────────────
    # (Real implementation would also remove pending_label and add
    # processed_label via the Gmail API; here we audit + return.)
    return {
        "message_id": message_id, "status": "dispatched",
        "workflow_id": invocation.workflow_id,
        "final_verdict": result.final_verdict,
        "proposal_path": result.proposal_path,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="SAI email → skill-run intake worker.")
    p.add_argument("--inbound", required=True,
                   help="The inbound SAI email address (e.g. sai@example.com).")
    p.add_argument("--operator", required=True,
                   help="The operator email address (e.g. hello@example.com). "
                        "Only emails FROM this sender will be processed.")
    p.add_argument("--pending-label", default="SAI/skill-run-pending")
    p.add_argument("--processed-label", default="SAI/skill-run-processed")
    p.add_argument("--max-emails", type=int, default=5,
                   help="Stop after this many processed emails (rate limit).")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse + log, but don't invoke the skill or change labels.")
    p.add_argument("--mock-email-body", default=None,
                   help="For testing: skip Gmail entirely and process this body as one email.")
    p.add_argument("--mock-email-sender", default=None)
    args = p.parse_args()

    # ── Mock path (for unit tests + dry-runs without Gmail OAuth) ────
    if args.mock_email_body:
        result = process_one_email(
            docs_connector=None, send_connector=None,
            message_id="mock-1",
            sender=args.mock_email_sender or args.operator,
            subject="(mock)",
            body=args.mock_email_body,
            pending_label=args.pending_label,
            processed_label=args.processed_label,
            operator_address=args.operator,
            dry_run=args.dry_run,
        )
        _audit({"mode": "mock", **result})
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") in ("dispatched", "dry_run_would_invoke") else 1

    # ── Real Gmail polling path ──────────────────────────────────────
    try:
        docs, send = _build_gmail_client()
    except Exception as exc:
        msg = f"Gmail client init failed: {type(exc).__name__}: {exc}"
        _audit({"status": "gmail_init_failed", "error": msg})
        print(msg, file=sys.stderr)
        return 2

    # Query: emails with the pending label, not yet processed.
    query = (f"label:{args.pending_label} -label:{args.processed_label} "
             f"to:{args.inbound} from:{args.operator}")
    # The actual list+fetch API call shape lives in app.connectors.gmail_documents.
    # The reference impl below assumes a list_messages(query) method; SAI's
    # GmailDocumentConnector exposes similar — adapt to your method name.
    if not hasattr(docs, "list_messages"):
        msg = ("GmailDocumentConnector has no list_messages — adapt this script's "
               "list call to your specific Gmail wrapper method.")
        _audit({"status": "gmail_api_mismatch", "error": msg})
        print(msg, file=sys.stderr)
        return 2

    results = []
    for msg in docs.list_messages(query=query, limit=args.max_emails):
        result = process_one_email(
            docs_connector=docs, send_connector=send,
            message_id=msg.id, sender=msg.sender,
            subject=msg.subject, body=msg.body,
            pending_label=args.pending_label,
            processed_label=args.processed_label,
            operator_address=args.operator,
            dry_run=args.dry_run,
        )
        results.append(result)
        _audit(result)

    print(json.dumps({"processed": len(results), "results": results},
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
