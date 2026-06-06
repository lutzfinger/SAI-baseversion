"""email_runner — multi-turn email trigger surface for the cost-compiler.

The operator emails the configured SAI@ address; the bot drives the
whole cost-compiler workflow with conversational follow-ups on the
SAME email thread until either:

  1. operator approves the staged plan → bot creates the invoice +
     reconciles + replies with final summary (status COMPLETED)
  2. operator cancels or replies "no" → bot acks + closes (DROPPED)
  3. 24h idle timeout (EXPIRED) — per SAI #16g pending intents

Conversation routing on each poll:

  • NEW trigger email (no existing intent for thread_id) →
    open intent, run cost_compiler_agent on the trigger text, reply
    with either a clarification or a staged-plan summary.

  • Reply to an AWAITING_CLARIFICATION intent →
    re-invoke cost_compiler_agent with the FULL conversation history
    so it has context (per #16g — never re-propose a rejected shape).

  • Reply to an AWAITING_APPROVAL intent →
    parse via approval.classify_reply.
      APPROVED → run post-approval steps (create-invoice, tag,
                 match-receipts, sense-check, reconcile),
                 reply with summary + invoice link.
      REJECTED → ack, set DROPPED.
      Feedback → keep AWAITING_APPROVAL open, reply with a friendly
                 "please reply yes or no" + recap so the operator can
                 try again (per #6a + #30).

Per SAI #5 (least-privileged), this listener requires:
  gmail.readonly  — for polling triggers
  gmail.send      — for replying

The skill keeps two tokens (`gmail_token.json` for the receipt
fetcher, separate `gmail_send_token.json` here) so we don't grant
send to the generic readers.

Configuration required in overlay (`identity.yaml`):

    email:
      trigger_label: "sai-trigger"
      from_address: "your+sai@example.com"
      operator_email: "your-personal@example.com"

Public API:
    listen(overlay, *, poll_interval=60.0)
    send_reply(overlay, original_msg, body, attachments=None)
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Optional


def _invoice_tool_active() -> bool:
    """True when the SAI dispatcher's invoice executor tool owns invoices (Stage 2
    cutover), so this daemon YIELDS invoice handling (one responder per command).
    Single toggle: ``SAI_INVOICE_TOOL`` in the process env OR in
    ``~/.config/sai/runtime.env`` (which the tagger also sources). Default OFF ->
    the daemon owns invoices exactly as before. Reversible by removing the flag."""
    truthy = {"1", "true", "on", "yes"}
    if os.environ.get("SAI_INVOICE_TOOL", "").strip().lower() in truthy:
        return True
    try:
        runtime_env = Path(os.path.expanduser("~/.config/sai/runtime.env"))
        for line in runtime_env.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("SAI_INVOICE_TOOL"):
                val = s.split("=", 1)[1].strip().strip('"').strip("'").lower()
                return val in truthy
    except Exception:  # noqa: BLE001 — missing/unreadable runtime.env -> daemon keeps invoices
        pass
    return False


GMAIL_SEND_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    # gmail.modify is needed for `messages().modify(removeLabelIds=...)`
    # so the daemon can mark processed emails as read and not loop on
    # them forever. Without this scope the modify call 403s.
    "https://www.googleapis.com/auth/gmail.modify",
]
DEFAULT_CREDS = "~/.SAI/credentials.json"
DEFAULT_SEND_TOKEN = "~/.SAI/gmail_send_token.json"


# ─── Gmail auth ────────────────────────────────────────────────────────

def _build_service(scopes: list[str] = GMAIL_SEND_SCOPES,
                   creds_path: str = DEFAULT_CREDS,
                   token_path: str = DEFAULT_SEND_TOKEN):
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        raise ImportError(
            "google-api-python-client + google-auth-oauthlib required. "
            "Install:  python3 -m pip install --user "
            "google-api-python-client google-auth-oauthlib\n"
            f"({e})"
        )

    creds_path = os.path.expanduser(creds_path)
    token_path = os.path.expanduser(token_path)
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds:
            if not os.path.exists(creds_path):
                raise FileNotFoundError(
                    f"No Google OAuth client at {creds_path}. Drop the "
                    "Google Cloud OAuth client JSON there first."
                )
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, scopes)
            creds = flow.run_local_server(port=0)
            Path(token_path).write_text(creds.to_json())
            os.chmod(token_path, 0o600)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ─── Gmail message helpers ────────────────────────────────────────────

def _list_label_id(svc, name: str) -> Optional[str]:
    resp = svc.users().labels().list(userId="me").execute()
    for lbl in resp.get("labels", []):
        if lbl.get("name") == name:
            return lbl["id"]
    return None


def _ensure_label_id(svc, name: str) -> Optional[str]:
    """Return the label id, creating the label if it doesn't exist."""
    lid = _list_label_id(svc, name)
    if lid:
        return lid
    try:
        created = svc.users().labels().create(
            userId="me",
            body={"name": name, "labelListVisibility": "labelShow",
                  "messageListVisibility": "show"},
        ).execute()
        return created.get("id")
    except Exception as e:  # noqa: BLE001
        print(f"  _ensure_label_id({name}) failed: {e!r}")
        return None


def _apply_status_label(svc, thread_id: str, add_label: str,
                        remove_legacy: tuple[str, ...] = ("SAI/Input",)) -> None:
    """Tag a handled thread with its status label (SAI/plan|done|proposal)
    and strip the legacy SAI/Input (defensive — the Gmail filter that
    auto-applied it was removed 2026-05-28, but old threads may carry it).
    Uses gmail.modify (already in GMAIL_SEND_SCOPES). Best-effort: a
    label failure never breaks the reply that already went out."""
    try:
        add_id = _ensure_label_id(svc, add_label)
        remove_ids = [i for i in
                      (_list_label_id(svc, n) for n in remove_legacy) if i]
        body: dict = {}
        if add_id:
            body["addLabelIds"] = [add_id]
        if remove_ids:
            body["removeLabelIds"] = remove_ids
        if body:
            svc.users().threads().modify(
                userId="me", id=thread_id, body=body).execute()
            print(f"  tagged thread {thread_id} -> {add_label} (−SAI/Input)")
    except Exception as e:  # noqa: BLE001
        print(f"  _apply_status_label failed (non-fatal): {e!r}")


def _decode_body(payload: dict) -> str:
    if not payload:
        return ""
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(
            payload["body"]["data"].encode()
        ).decode(errors="replace")
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                return base64.urlsafe_b64decode(
                    data.encode()
                ).decode(errors="replace")
    for part in payload.get("parts", []) or []:
        text = _decode_body(part)
        if text:
            return text
    return ""


def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers", []) or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _is_operator_sender(sender: str, operator_email: str,
                        trigger_address: str = "") -> bool:
    """Does this From belong to the operator?

    The daemon's OWN messages are excluded at the call site by the X-SAI-Bot
    header (and bot_sent_message_ids), NOT by address - because the operator's
    `sai@` alias and the daemon both send from `sai@`. This is the operator
    allowlist, widened to include the `sai@` trigger alias, so a reply sent from
    EITHER the operator's primary (hello@) OR their sai@ alias is recognized.
    Identification is therefore header-first; address is only the allowlist.
    """
    s = (sender or "").lower()
    return bool((operator_email and operator_email.lower() in s)
                or (trigger_address and trigger_address.lower() in s))


def send_reply(overlay: dict, original_msg: dict, body: str,
               attachments: Optional[list[Path]] = None) -> str:
    """Send an email reply on the same thread as `original_msg`.

    Returns the Gmail message ID of the sent message so callers can
    record it in the intent's `bot_sent_message_ids` and the poll loop
    can skip it (avoiding the bot re-reading its own replies as
    operator replies — both share the same authenticated Gmail
    account's From address).
    """
    svc = _build_service()
    to_addr = _header(original_msg, "From")
    subject = _header(original_msg, "Subject")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["From"] = (overlay.get("email") or {}).get("from_address", "")
    msg["Subject"] = subject
    msg["In-Reply-To"] = _header(original_msg, "Message-Id")
    msg["References"] = (
        _header(original_msg, "References")
        or _header(original_msg, "Message-Id")
    )
    # Header fingerprint as a belt-and-suspenders signal alongside
    # bot_sent_message_ids tracking.
    msg["X-SAI-Bot"] = "cost-compiler/1"
    msg.set_content(body)
    for path in attachments or []:
        data = Path(path).read_bytes()
        maintype, subtype = ("application", "octet-stream")
        if Path(path).suffix.lower() == ".md":
            maintype, subtype = ("text", "markdown")
        elif Path(path).suffix.lower() == ".pdf":
            maintype, subtype = ("application", "pdf")
        msg.add_attachment(
            data, maintype=maintype, subtype=subtype,
            filename=Path(path).name,
        )
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    resp = svc.users().messages().send(
        userId="me",
        body={"raw": raw, "threadId": original_msg.get("threadId")},
    ).execute()
    return resp.get("id", "")


# ─── main poll loop ───────────────────────────────────────────────────

def listen(overlay: dict, *, poll_interval: float = 60.0) -> None:
    """Block forever, polling Gmail for triggers AND replies on open intents."""
    from lib import email_intents, cost_compiler_agent

    cfg = overlay.get("email") or {}
    trigger_address = cfg.get("trigger_address")
    label_name = cfg.get("trigger_label")  # legacy gate
    operator_email = cfg.get("operator_email")
    if not operator_email:
        raise RuntimeError(
            "Overlay is missing email.operator_email. Configure your "
            "overlay's identity.yaml before running email-listen."
        )
    if not (trigger_address or label_name):
        raise RuntimeError(
            "Overlay missing both email.trigger_address (preferred — "
            "plus-addressing, zero setup) and email.trigger_label "
            "(legacy — requires a Gmail label). Set one in identity.yaml."
        )

    svc = _build_service()
    label_id: Optional[str] = None
    if label_name:
        label_id = _list_label_id(svc, label_name)
        if not label_id:
            raise RuntimeError(
                f"Gmail label {label_name!r} not found. Either create it "
                f"in Gmail or switch to email.trigger_address."
            )

    print(f"email_runner: listening")
    if trigger_address:
        print(f"  trigger:   to:{trigger_address}")
    if label_name:
        print(f"  label:     {label_name!r}")
    print(f"  operator:  {operator_email!r}")
    print(f"  poll:      {poll_interval}s")
    print(f"  intents:   {email_intents._state_root()}")

    seen_message_ids: set[str] = set()
    while True:
        try:
            _poll_new_triggers(svc, overlay, label_id, label_name,
                                trigger_address, operator_email,
                                seen_message_ids)
            _poll_open_intent_replies(svc, overlay, operator_email,
                                       seen_message_ids)
            email_intents.expire_idle_intents()
        except Exception as e:
            print(f"email_runner: poll error: {e}")
        time.sleep(poll_interval)


def _poll_new_triggers(
    svc, overlay: dict, label_id: Optional[str], label_name: Optional[str],
    trigger_address: Optional[str], operator_email: str,
    seen_message_ids: set[str],
) -> None:
    """Look for new emails to sai@. For each, route via dispatch_agent
    which classifies into 5 verdicts:

      COST_COMPILER       → run cost_compiler_agent (existing flow)
      EVAL_FEEDBACK       → log + send brief confirmation reply
      GENERAL_QUERY       → invoke general_assistant (Claude+web_search)
      WORKFLOW_SUGGESTION → invoke general_assistant.propose_workflow
      IGNORE              → silent file, no reply (pure noise)

    Per operator decision 2026-05-20: `sai@` is a full Claude-via-email
    interface, not just a cost-compiler trigger. Everything the
    operator might ask gets a reply (unless it's clearly noise)."""
    from lib import email_intents, cost_compiler_agent, dispatch_agent
    from lib import general_assistant, email_format

    # Prefer plus-addressing (`to:`); fall back to label gate.
    # The `newer_than:` clause is a safety net: even if the daemon was
    # offline for weeks, the first poll after restart only picks up
    # recent mail, not the whole history. Overlay-configurable.
    newer_than = (overlay.get("email") or {}).get("newer_than", "2d")
    recency = f"newer_than:{newer_than}" if newer_than else ""
    if trigger_address:
        q = f"to:{trigger_address} is:unread from:{operator_email} {recency}".strip()
    else:
        q = f"label:{label_name} is:unread from:{operator_email} {recency}".strip()
    resp = svc.users().messages().list(
        userId="me", q=q, maxResults=20,
    ).execute()
    for ref in resp.get("messages", []) or []:
        msg_id = ref["id"]
        if msg_id in seen_message_ids:
            continue
        msg = svc.users().messages().get(
            userId="me", id=msg_id, format="full",
        ).execute()
        thread_id = msg.get("threadId", msg_id)
        # Don't open a duplicate intent if the thread already has one.
        # Mark as read (Gmail-side) so future polls of `is:unread`
        # don't keep surfacing it. Do NOT add to seen_message_ids —
        # `_poll_open_intent_replies` must still be able to see this
        # message ID so it can route the reply through the agent.
        if email_intents.load(thread_id):
            try:
                remove = ["UNREAD"]
                if label_id:
                    remove.append(label_id)
                svc.users().messages().modify(
                    userId="me", id=msg_id,
                    body={"removeLabelIds": remove},
                ).execute()
            except Exception:
                pass
            continue
        subject = _header(msg, "Subject")
        body = _decode_body(msg.get("payload") or {})
        trigger_text = (subject + "\n" + body).strip()
        print(f"\n=== New email on thread {thread_id} ===")
        print(f"  subject: {subject[:80]}")

        # Dispatch the email (rules + Haiku) into one of 5 verdicts.
        # Per operator preference 2026-05-20: sai@ is a full Claude
        # interface — EVERY operator-authored email gets some reply,
        # only true noise is silently filed.
        dispatch = dispatch_agent.classify(subject, body, overlay)
        dispatch_agent.log_dispatch(thread_id, msg_id, subject, dispatch)
        print(f"  dispatch: {dispatch.verdict.value}  "
              f"({dispatch.source}, conf={dispatch.confidence})")

        # Mark read first (regardless of verdict — we've now processed it)
        try:
            remove = ["UNREAD"]
            if label_id:
                remove.append(label_id)
            svc.users().messages().modify(
                userId="me", id=msg_id,
                body={"removeLabelIds": remove},
            ).execute()
        except Exception:
            pass

        if dispatch.verdict is dispatch_agent.Verdict.IGNORE:
            print(f"  → silently filed")
            seen_message_ids.add(msg_id)
            continue

        if dispatch.verdict is dispatch_agent.Verdict.EVAL_FEEDBACK:
            # Log only. The SAI tagger/dispatcher (apply_operator_tag_error_floor ->
            # tag_error_execution) now OWNS the operator-facing reply for label
            # corrections (it re-tags the original + writes the eval row + replies with
            # what it did). The daemon must NOT also reply here, or the operator gets two
            # replies per correction. (2026-06-05 consolidation: one responder per command.)
            dispatch_agent.log_eval_feedback(thread_id, msg_id, subject, body)
            print(f"  → eval_feedback_inbox.jsonl (reply owned by SAI tag_error dispatch)")
            seen_message_ids.add(msg_id)
            continue

        if dispatch.verdict is dispatch_agent.Verdict.GENERAL_QUERY:
            # Yielded to the SAI dispatcher's general_query tool (LLM + web_search,
            # app/tools/operator_qa) — one responder per command. The daemon no longer
            # answers operator questions; the dispatcher owns them. (2026-06-05 fold-in.)
            print("  → general_query yielded to SAI dispatcher (operator_qa tool)")
            seen_message_ids.add(msg_id)
            continue

        if dispatch.verdict is dispatch_agent.Verdict.WORKFLOW_SUGGESTION:
            print(f"  → workflow_suggestion (case b — no tools)")
            try:
                proposal = general_assistant.propose_workflow(
                    text=f"Subject: {subject}\n\n{body}",
                    overlay=overlay,
                )
            except Exception as e:
                proposal = (
                    f"I couldn't draft a workflow proposal (internal "
                    f"error: {e}). Try rephrasing what you'd want it to do."
                )
            send_reply(overlay, msg, email_format.workflow_suggestion_reply(proposal))
            seen_message_ids.add(msg_id)
            continue

        if dispatch.verdict is dispatch_agent.Verdict.AD_HOC_CAPABLE:
            print(f"  → ad_hoc_capable (case c — auto-executing low-risk steps)")
            request_text = f"Subject: {subject}\n\n{body}"
            from lib import ad_hoc_decomposed
            # Turn-1 AUTO-EXECUTE: do the low-risk work now (create the
            # draft / calendar block), reply terse, tag SAI/plan. Only
            # truly non-doable tasks fall back to the propose path.
            status_label = "SAI/proposal"
            try:
                result = ad_hoc_decomposed.auto_execute_ad_hoc(
                    text=request_text,
                    overlay=overlay,
                    claude_loop_fn=general_assistant._run_claude_loop,
                )
            except Exception as e:
                result = {"reply_text": None, "status_label": "SAI/proposal",
                          "did_write": False, "kind": "error"}
                print(f"  auto_execute error: {e!r}")
            if result.get("reply_text"):
                reply_text2 = result["reply_text"]
                status_label = result.get("status_label", "SAI/plan")
            else:
                # Not auto-executable → fall back to the propose path.
                try:
                    reply_text2 = general_assistant.propose_ad_hoc_steps(
                        text=request_text, overlay=overlay)
                except Exception as e:
                    reply_text2 = (f"I couldn't handle this one (internal "
                                   f"error: {e}). Try rephrasing.")
                status_label = "SAI/proposal"
            intent = email_intents.open_intent(
                thread_id=thread_id,
                operator_email=operator_email,
                trigger_subject=subject,
                first_text=request_text,
                intent_kind="ad_hoc",
                initial_status=email_intents.IntentStatus.AWAITING_APPROVAL,
            )
            intent.ad_hoc_original_request = request_text
            intent.ad_hoc_last_proposal = reply_text2
            intent.processed_operator_message_ids.append(msg_id)
            email_intents.save(intent)
            reply_body = email_format.ad_hoc_proposal_reply(reply_text2)
            sent_id = send_reply(overlay, msg, reply_body)
            if sent_id:
                intent.bot_sent_message_ids.append(sent_id)
            email_intents.append_bot_message(intent, reply_body)
            email_intents.save(intent)
            # Tag the thread with the status label (remove legacy SAI/Input).
            _apply_status_label(svc, thread_id, status_label)
            seen_message_ids.add(msg_id)
            continue

        if dispatch.verdict is dispatch_agent.Verdict.INVOICE_DRAFT:
            if _invoice_tool_active():
                print("  → invoice_draft YIELDED to the SAI dispatcher invoice tool (SAI_INVOICE_TOOL)")
                seen_message_ids.add(msg_id)
                continue
            print(f"  → invoice_draft (draft UNSENT invoice + ask approval)")
            from lib import invoice_intent
            try:
                invoice_intent.handle_new_invoice_trigger(
                    svc, overlay, msg, subject, body)
            except Exception as e:  # noqa: BLE001
                print(f"  invoice_draft error: {e!r}")
                try:
                    send_reply(overlay, msg,
                               f"I hit an error drafting that invoice ({e}). "
                               "Nothing was created or sent.")
                except Exception:
                    pass
            seen_message_ids.add(msg_id)
            continue

        # Otherwise: COST_COMPILER — open an intent + invoke the agent
        # (the existing flow).
        intent = email_intents.open_intent(
            thread_id=thread_id,
            operator_email=operator_email,
            trigger_subject=subject,
            first_text=trigger_text,
        )
        # Mark the trigger's own message id as already processed so
        # the open-intent poll doesn't re-route it as if it were a new
        # operator reply on the next restart.
        intent.processed_operator_message_ids.append(msg_id)
        email_intents.save(intent)
        _drive_agent_turn(svc, overlay, intent, msg, fresh_text=trigger_text)
        # (mark-read already happened at the top of the dispatch block)
        seen_message_ids.add(msg_id)


def _poll_open_intent_replies(
    svc, overlay: dict, operator_email: str, seen_message_ids: set[str],
) -> None:
    """For each open intent, check its thread for new operator messages.

    Skips the bot's own past replies by checking:
      1. `intent.bot_sent_message_ids` (tracked at send time)
      2. The `X-SAI-Bot` header (belt-and-suspenders for replies sent
         before the tracking was added)
    """
    from lib import email_intents

    trigger_address = (overlay.get("email") or {}).get("trigger_address") or ""
    open_map = email_intents.open_threads()
    if not open_map:
        return
    for thread_id, status in open_map.items():
        intent = email_intents.load(thread_id)
        if not intent:
            continue
        bot_ids = set(intent.bot_sent_message_ids)
        # Durable processed-op-ids: persisted across daemon restarts.
        # Combined with the in-memory seen_message_ids (cheap dedup
        # within a single process lifetime).
        processed_op_ids = set(intent.processed_operator_message_ids)
        try:
            thread = svc.users().threads().get(
                userId="me", id=thread_id, format="full",
            ).execute()
        except Exception as e:
            print(f"  (couldn't fetch thread {thread_id}: {e})")
            continue
        messages = thread.get("messages") or []
        messages.sort(key=lambda m: int(m.get("internalDate", "0")))
        latest_op_msg = None
        for m in messages:
            if m["id"] in seen_message_ids:
                continue
            if m["id"] in bot_ids:
                continue  # bot's own message
            if m["id"] in processed_op_ids:
                continue  # operator msg we already routed (durable)
            if _header(m, "X-SAI-Bot"):
                continue  # fingerprint says it's ours (header-first self-check)
            # Operator reply = not-ours (above) AND from one of the operator's
            # addresses (primary OR the sai@ alias). Without the alias, a reply
            # the operator's client sent from sai@ was silently ignored.
            if _is_operator_sender(_header(m, "From"), operator_email, trigger_address):
                latest_op_msg = m
        if not latest_op_msg:
            for m in messages:
                seen_message_ids.add(m["id"])
            continue
        body = _decode_body(latest_op_msg.get("payload") or {})
        reply_text = body.strip()
        print(f"\n=== Reply on open intent {thread_id} (status={status.value}) ===")
        print(f"  reply (first 80): {reply_text[:80]}")
        _route_reply(svc, overlay, intent, latest_op_msg, reply_text)
        # Mark this operator msg id as durably processed.
        intent.processed_operator_message_ids.append(latest_op_msg["id"])
        email_intents.save(intent)
        for m in messages:
            seen_message_ids.add(m["id"])


def _send_and_track(overlay: dict, intent, original_msg: dict, body: str,
                    attachments: Optional[list[Path]] = None) -> None:
    """Send a reply AND track the returned message ID in the intent so
    the next poll doesn't read the bot's own message as an operator
    reply."""
    from lib import email_intents
    msg_id = send_reply(overlay, original_msg, body, attachments=attachments)
    if msg_id:
        intent.bot_sent_message_ids.append(msg_id)
    email_intents.append_bot_message(intent, body)
    email_intents.save(intent)


def _route_reply(svc, overlay: dict, intent, original_msg: dict, reply_text: str) -> None:
    """Route a reply based on the intent's current status AND kind."""
    from lib import email_intents, approval, email_format

    email_intents.append_operator_message(intent, reply_text)

    # Invoice-draft branch — approve sends, reject deletes. Must precede the
    # cost_compiler AWAITING_APPROVAL block (which assumes staged_plan_path).
    if intent.intent_kind == "invoice":
        if _invoice_tool_active():
            print("  → invoice reply YIELDED to the SAI dispatcher invoice tool (SAI_INVOICE_TOOL)")
            return
        from lib import invoice_intent
        invoice_intent.handle_invoice_reply(svc, overlay, intent, original_msg, reply_text)
        return

    # Case (c) AD_HOC branch — its own approve/reject path; the
    # existing AWAITING_APPROVAL block below is cost_compiler-specific
    # (it references staged_plan_path and the QB-invoice apology copy).
    if intent.intent_kind == "ad_hoc":
        if intent.status == email_intents.IntentStatus.AWAITING_APPROVAL:
            _route_ad_hoc_reply(overlay, intent, original_msg, reply_text)
            return
        if intent.status == email_intents.IntentStatus.EXECUTING:
            _send_and_track(overlay, intent, original_msg,
                email_format.ad_hoc_proposal_reply(
                    "Still running your approved steps — I'll reply with the result as soon as it's done."
                ),
            )
            return
        # COMPLETED/DROPPED/EXPIRED ad_hoc intents shouldn't be routed
        # to from the open-intent poll (open_threads() filters them out)
        # — defensive ack so we don't drop the reply silently (#16e).
        _send_and_track(overlay, intent, original_msg,
            email_format.ad_hoc_proposal_reply(
                "This ad-hoc thread is already closed. Send a fresh email to start a new one."
            ),
        )
        return

    if intent.status == email_intents.IntentStatus.AWAITING_CLARIFICATION:
        # Re-invoke the agent with the FULL conversation history so it
        # has prior context (per #16g — no re-proposing a rejected shape).
        _drive_agent_turn(svc, overlay, intent, original_msg,
                          fresh_text=reply_text)
        return

    if intent.status == email_intents.IntentStatus.AWAITING_APPROVAL:
        verdict = approval.classify_reply(reply_text)
        if verdict == approval.ApprovalState.APPROVED:
            _on_approval(svc, overlay, intent, original_msg)
            return
        if verdict == approval.ApprovalState.REJECTED:
            _send_and_track(overlay, intent, original_msg,
                f"Got it — I won't proceed with the invoice. "
                f"Plan stays staged at:\n{intent.staged_plan_path}\n\n"
                f"Reply with new instructions if you'd like me to "
                f"recompute, or send a fresh trigger to start over.",
            )
            email_intents.set_status(intent, email_intents.IntentStatus.DROPPED)
            return
        # Feedback — keep open, ask for explicit yes/no.
        _send_and_track(overlay, intent, original_msg,
            "I heard your note, but to keep the audit log clean I "
            "need an explicit 'yes' or 'no' on the staged plan. "
            "Reply 'yes' to create the invoice, 'no' to drop it, or "
            "include 'no' followed by what you'd want changed and I'll "
            "rerun.",
        )
        return


def _route_ad_hoc_reply(overlay: dict, intent, original_msg: dict,
                        reply_text: str) -> None:
    """Approve/reject/feedback handling for an ad_hoc (case c) intent.

    On approve: mark EXECUTING, call execute_ad_hoc_steps, send the
    case-(a)-shaped status reply, mark COMPLETED.
    On reject:  short ack, mark DROPPED.
    On feedback: keep open, re-ask for explicit y/n (#16g pending
                 intents — never silently drop)."""
    from lib import email_intents, approval, general_assistant, email_format
    from lib import ad_hoc_decomposed

    verdict = approval.classify_reply(reply_text)

    # --- Auto-exec intent feedback (2026-05-28) --------------------------
    # Turn-1 already DID the low-risk work (created the draft / calendar
    # block) and the reply opened with "Auto Execution". So feedback now
    # means: bare approve = "already done"; steering ("no, use X@y.com")
    # or edits = RE-RUN the auto-exec with the feedback (re-draft to the
    # corrected recipient / redo the block); bare reject = drop + tell the
    # operator to delete the artifact. Steering is checked before reject
    # so "no ... <email>" re-targets instead of dropping (#16g).
    last_proposal = intent.ad_hoc_last_proposal or ""
    if last_proposal.startswith("Auto Execution"):
        steering = ad_hoc_decomposed.extract_email(reply_text)
        bare_approve = verdict == approval.ApprovalState.APPROVED and not steering
        bare_reject = verdict == approval.ApprovalState.REJECTED and not steering
        if bare_approve:
            _send_and_track(overlay, intent, original_msg,
                email_format.ad_hoc_proposal_reply(
                    "Already done — it's in your Drafts / on your calendar, "
                    "ready for you. Nothing else needed."))
            email_intents.set_status(intent, email_intents.IntentStatus.COMPLETED)
            return
        if bare_reject:
            _send_and_track(overlay, intent, original_msg,
                email_format.ad_hoc_proposal_reply(
                    "Dropped. If a draft or calendar block was created, just "
                    "delete it from Drafts / Calendar."))
            email_intents.set_status(intent, email_intents.IntentStatus.DROPPED)
            return
        # steering / edits → re-run auto-exec with the feedback folded in
        email_intents.set_status(intent, email_intents.IntentStatus.EXECUTING)
        try:
            result = ad_hoc_decomposed.auto_execute_ad_hoc(
                text=intent.ad_hoc_original_request or "",
                overlay=overlay,
                claude_loop_fn=general_assistant._run_claude_loop,
                operator_feedback=reply_text,
            )
            reply2 = result.get("reply_text") or (
                "I couldn't redo that — tell me the exact recipient or time "
                "and I'll fix it.")
        except Exception as e:  # noqa: BLE001
            reply2 = (f"I couldn't redo it (internal error: {e}). Tell me the "
                      "exact recipient or time.")
        intent.ad_hoc_last_proposal = reply2
        _send_and_track(overlay, intent, original_msg,
                        email_format.ad_hoc_proposal_reply(reply2))
        email_intents.save(intent)
        # keep open for further steering
        email_intents.set_status(intent, email_intents.IntentStatus.AWAITING_APPROVAL)
        return
    # --- end auto-exec intent feedback -----------------------------------

    if verdict == approval.ApprovalState.APPROVED:
        email_intents.set_status(intent, email_intents.IntentStatus.EXECUTING)
        try:
            result_text = general_assistant.execute_ad_hoc_steps(
                approved_proposal=intent.ad_hoc_last_proposal or "",
                original_request=intent.ad_hoc_original_request or "",
                overlay=overlay,
            )
        except Exception as e:
            result_text = (
                f"Partial — execution failed with internal error: {e}. "
                "No write actions were taken (case-c steps are read-only). "
                "Try rephrasing or come back in a few minutes."
            )
        _send_and_track(overlay, intent, original_msg,
                        email_format.ad_hoc_proposal_reply(result_text))
        email_intents.set_status(intent, email_intents.IntentStatus.COMPLETED)
        return
    if verdict == approval.ApprovalState.REJECTED:
        _send_and_track(overlay, intent, original_msg,
            email_format.ad_hoc_proposal_reply(
                "Got it — dropping. Send a fresh email if you'd like me to try a different approach."
            ),
        )
        email_intents.set_status(intent, email_intents.IntentStatus.DROPPED)
        return
    # Feedback — keep AWAITING_APPROVAL open; re-ask for explicit y/n.
    _send_and_track(overlay, intent, original_msg,
        email_format.ad_hoc_proposal_reply(
            "I heard your note — to keep the audit log clean I need a "
            "single 'y' or 'n' on the STEPS above. Reply 'y' to run "
            "them, 'n' to drop, or add what you'd want changed and "
            "I'll re-propose."
        ),
    )

    if intent.status == email_intents.IntentStatus.EXECUTING:
        # Operator messaged us mid-execution. Acknowledge but don't
        # interrupt — the post-approval steps are running.
        _send_and_track(overlay, intent, original_msg,
            "I'm in the middle of executing your plan. I'll reply "
            "with the result as soon as it's done.",
        )
        return


def _drive_agent_turn(svc, overlay: dict, intent, original_msg: dict,
                      fresh_text: str) -> None:
    """Invoke the cost_compiler_agent with the full conversation history
    + the operator's latest message, then reply with either a
    clarification or a staged-plan summary."""
    from lib import email_intents, cost_compiler_agent

    # Build a context-rich input: prior history + latest message.
    conversation = email_intents.conversation_summary(intent)
    agent_input = (
        f"── prior conversation on this email thread ──\n"
        f"{conversation}\n\n"
        f"── operator's latest message (decide what to do next) ──\n"
        f"{fresh_text}"
    )
    result = cost_compiler_agent.run_agent(
        source_text=agent_input,
        overlay=overlay,
    )
    inv = result.invocation
    if inv:
        email_intents.record_agent_invocation(
            intent,
            invocation_id=inv.invocation_id,
            iterations=inv.iterations,
            cost_usd=inv.cost_usd,
            staged_plan_path=inv.staged_plan_path,
            proposal_id=inv.proposal_id,
        )

    from lib import email_format

    if not result.staged_plan_path:
        # Clarification reply. Keep AWAITING_CLARIFICATION. The agent's
        # text is usually already conversational; just pass through.
        _send_and_track(overlay, intent, original_msg, result.operator_message)
        return

    # Plan staged — run the collect phase, then prompt for approval.
    intent.staged_plan_path = result.staged_plan_path
    email_intents.save(intent)
    _send_and_track(overlay, intent, original_msg,
        email_format.plan_staged(result.proposed_plan,
                                 result.operator_message)
    )
    _run_collect_phase_and_ask_approval(
        svc, overlay, intent, original_msg, result.proposed_plan,
    )


def _run_collect_phase_and_ask_approval(
    svc, overlay: dict, intent, original_msg: dict, proposed_plan: dict,
) -> None:
    """Run the read-only collect phase (scan-cards, search-receipts,
    attach-onsite-photos, extract-pre-bookings), build a friendly
    review summary, send it on the thread, set AWAITING_APPROVAL."""
    from lib import email_intents, email_format

    slug = proposed_plan["trip_slug"]
    start = proposed_plan["trip_start"]
    end = proposed_plan["trip_end"]
    customer = proposed_plan["customer"]["DisplayName"]
    currency = proposed_plan["invoice_currency"]

    # Two kinds of steps:
    #   blocking — must succeed; failure aborts the plan (DROPPED)
    #   advisory — nice-to-have; failure is logged but doesn't block
    #              moving to AWAITING_APPROVAL. The operator can re-run
    #              the advisory step manually from CLI.
    #
    # `attach-onsite-photos` is advisory because:
    #   (1) macOS TCC on launchd-spawned subprocesses sometimes blocks
    #       writes to ~/Downloads/ even when the parent has access
    #   (2) it's a "render-PDFs-for-review" step, not a "QB write" step
    #   (3) the operator can run it interactively in ~5 seconds
    # `extract-pre-bookings` is advisory for similar reasons.
    # Daemon-safe output root. macOS TCC blocks launchd-spawned
    # subprocesses from writing to ~/Downloads/ even when the parent
    # daemon has access (the permission doesn't inherit through
    # subprocess.run for some launchd configurations). Route PDFs to
    # the SAI Application Support folder where the daemon CAN write,
    # then a post-step (run in operator's interactive context) copies
    # them to ~/Downloads/ if desired.
    daemon_out_root = "~/Library/Application Support/SAI/receipt-collector/downloads"
    pre_steps = [
        ("scan-cards", {"start": start, "end": end}, "blocking"),
        ("search-receipts", {"start": start, "end": end}, "blocking"),
        ("attach-onsite-photos", {"start": start, "end": end,
                                   "trip": slug, "customer": customer,
                                   "no-upload": True,
                                   "out-root": daemon_out_root}, "advisory"),
        ("extract-pre-bookings", {"start": start, "end": end,
                                   "customer": customer}, "advisory"),
    ]
    # Structured log of each step's result — fed to email_format for
    # human-readable rendering after the loop. Also keep a raw stderr
    # of the last failed step for diagnosis.
    steps_log: list[dict] = []
    advisory_failures: list[str] = []
    last_blocking_error: str = ""

    for i, (name, kwargs, criticality) in enumerate(pre_steps, 1):
        cmd = [sys.executable, "-m", "skills.receipt-collector.runner", name]
        for k, v in (kwargs or {}).items():
            if v is None or v is False:
                continue
            flag = f"--{k.replace('_', '-')}"
            if v is True:
                cmd.append(flag)
            else:
                cmd.append(flag)
                cmd.append(str(v))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=600)
        except subprocess.TimeoutExpired:
            steps_log.append({
                "name": name, "exit_code": "timeout", "ok": False,
                "criticality": criticality,
                "summary_line": "timed out",
            })
            if criticality == "blocking":
                _send_and_track(overlay, intent, original_msg,
                    email_format.collect_phase_failed(slug, steps_log,
                        last_error="subprocess timed out"))
                email_intents.set_status(intent, email_intents.IntentStatus.DROPPED)
                return
            advisory_failures.append(f"{name} (timed out)")
            continue

        # Capture a one-line summary for human-readable use
        summary_line = ""
        if name == "scan-cards":
            for ln in (proc.stdout or "").splitlines():
                if ln.startswith("Purchases "):
                    summary_line = ln
                    break
        elif name == "search-receipts":
            summary_line = "Gmail receipt-sender search built"
        elif name == "attach-onsite-photos":
            for ln in (proc.stdout or "").splitlines():
                if ln.startswith("Summary:") and "photo-PDFs" in ln:
                    summary_line = ln
                    break
        elif name == "extract-pre-bookings":
            for ln in (proc.stdout or "").splitlines():
                if "pre-booking" in ln.lower() or "candidate" in ln.lower():
                    summary_line = ln
                    break

        steps_log.append({
            "name": name,
            "exit_code": proc.returncode,
            "ok": proc.returncode == 0,
            "criticality": criticality,
            "summary_line": summary_line,
        })

        if proc.returncode != 0:
            if criticality == "blocking":
                last_blocking_error = proc.stderr or ""
                _send_and_track(overlay, intent, original_msg,
                    email_format.collect_phase_failed(slug, steps_log,
                        last_error=last_blocking_error))
                email_intents.set_status(intent, email_intents.IntentStatus.DROPPED)
                return
            else:
                advisory_failures.append(
                    f"{name} (exit {proc.returncode}) — operator can re-run manually"
                )

    # Build the friendly review summary
    _send_and_track(overlay, intent, original_msg,
        email_format.collect_phase_done(
            slug=slug, customer=customer, start=start, end=end,
            steps_log=steps_log,
            staged_plan_path=intent.staged_plan_path,
            advisory_failures=advisory_failures,
        )
    )
    email_intents.set_status(intent, email_intents.IntentStatus.AWAITING_APPROVAL)


def _build_final_review_text(
    slug: str, start: str, end: str, customer: str, currency: str,
    staged_path: Optional[str],
) -> str:
    return (
        f"Review:\n"
        f"  trip:      {slug}\n"
        f"  customer:  {customer}\n"
        f"  window:    {start} .. {end}\n"
        f"  currency:  {currency}\n"
        f"  staged:    {staged_path}\n"
    )


def _on_approval(svc, overlay: dict, intent, original_msg: dict) -> None:
    """Operator said yes — run the post-approval steps."""
    from lib import email_intents
    email_intents.set_status(intent, email_intents.IntentStatus.EXECUTING)
    if not intent.staged_plan_path:
        _send_and_track(overlay, intent, original_msg,
            "Can't proceed — there's no staged plan to act on. Send a fresh trigger.",
        )
        email_intents.set_status(intent, email_intents.IntentStatus.DROPPED)
        return

    # Read the staged plan to extract its parameters
    import json as _json
    try:
        plan = _json.loads(Path(intent.staged_plan_path).read_text())
    except Exception as e:
        _send_and_track(overlay, intent, original_msg, f"Couldn't read staged plan: {e}")
        email_intents.set_status(intent, email_intents.IntentStatus.DROPPED)
        return

    slug = plan["trip_slug"]
    start = plan["trip_start"]
    end = plan["trip_end"]
    customer = plan["customer"]["DisplayName"]
    currency = plan["invoice_currency"]

    # Post-approval steps. Per operator constraint 2026-05-20:
    # "DO NOT manage any other updates EXCEPT The creation (but not
    # sending) of an invoice." → run ONLY:
    #   1. match-receipts-to-purchases --no-upload (PDFs to disk; no
    #      QB attach, no tag, no memo)
    #   2. create-invoice (creates if not exists; idempotent marker)
    # Skipped: tag-purchases, sense-check, reconcile-billables.
    #
    # `create-invoice` needs a plan.json with `invoice_lines`. We
    # use the trip-runs folder's plan.json if present; otherwise we
    # fall back to the agent's proposed_plan.json (the runner will
    # error with a clear message about missing invoice_lines, which
    # surfaces back on the email thread).
    import os as _os
    overlay_root = _os.path.expanduser(
        overlay.get("trip_runs_root")
        or "~/Lutz_Dev/SAI/skills/receipt-collector/trip_runs"
    )
    candidate_plan = _os.path.join(overlay_root, slug, "plan.json")
    invoice_plan_path = (
        candidate_plan if _os.path.exists(candidate_plan)
        else intent.staged_plan_path
    )
    post_steps = [
        ("match-receipts-to-purchases",
         {"trip": slug, "customer": customer,
          "start": start, "end": end, "no-upload": True,
          "out-root": "~/Library/Application Support/SAI/receipt-collector/downloads"}),
        ("create-invoice",
         {"trip": slug, "plan": invoice_plan_path,
          "currency": currency}),
    ]
    from lib import email_format

    steps_log: list[dict] = []
    invoice_id: Optional[str] = None
    invoice_total: Optional[str] = None
    for i, (name, kwargs) in enumerate(post_steps, 1):
        cmd = [sys.executable, "-m", "skills.receipt-collector.runner", name]
        for k, v in (kwargs or {}).items():
            if v is None or v is False:
                continue
            flag = f"--{k.replace('_', '-')}"
            if v is True:
                cmd.append(flag)
            else:
                cmd.append(flag)
                cmd.append(str(v))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=600)
        except subprocess.TimeoutExpired:
            steps_log.append({
                "name": name, "exit_code": "timeout", "ok": False,
            })
            break
        steps_log.append({
            "name": name,
            "exit_code": proc.returncode,
            "ok": proc.returncode == 0,
        })
        # Try to extract invoice id from create-invoice stdout
        if name == "create-invoice" and proc.stdout:
            import re as _re
            m = _re.search(r"Invoice (?:Id=|Id\s*=\s*)(\d+).*?Total=([\d.,]+)",
                           proc.stdout)
            if m:
                invoice_id = m.group(1)
                invoice_total = m.group(2)
            else:
                # Idempotent skip — invoice already exists
                m2 = _re.search(r"SKIP.*Id=(\d+).*Total=([\d.,]+)",
                                proc.stdout)
                if m2:
                    invoice_id = m2.group(1)
                    invoice_total = m2.group(2)
        if proc.returncode not in (0, 1, 2):
            break

    _send_and_track(overlay, intent, original_msg,
        email_format.invoice_done(
            invoice_id=invoice_id,
            total=invoice_total,
            currency=currency,
            downloads_dir=(
                "~/Library/Application Support/SAI/receipt-collector/downloads/"
                f"sai-receipts-{slug}/"
            ),
            steps_log=steps_log,
        )
    )
    email_intents.set_status(intent, email_intents.IntentStatus.COMPLETED)
