"""invoice_intent — headless "send an invoice to <name> over <amount>" flow.

Runs inside the sai@ receipt-collector daemon. Turn 1 (auto, low-risk):
parse, resolve who <name> is (inbox + QBO fallback), create the customer if
missing, create the invoice UNSENT, email the summary in-thread, open an
AWAITING_APPROVAL intent. On the operator's APPROVE reply -> send (CC
hello@example.com); REJECT -> delete the unsent draft; ambiguous -> ask y/n.

The SEND is the ONLY gated side effect (PRINCIPLES #2/#7a: it never fires
without the operator's explicit reply). Pure logic comes from the one source of
truth via invoice_logic_bridge (#33a coupling, loose-ended). email_intents is
the authoritative approval state; PendingStore is the idempotency ledger that
also carries the send params (customer email + cc).
"""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from typing import Callable, Optional

from lib import email_intents
from lib import invoice_logic_bridge as L
from lib.qb_client import QBClient

CC_ADDRESS = "hello@example.com"
AUDIT_PATH = os.path.expanduser("~/Library/Logs/SAI/invoice-draft-and-send.jsonl")
PENDING_DB = os.path.expanduser(
    "~/Library/Application Support/SAI/invoice-draft-and-send/pending.db"
)


# ─── construction helpers ─────────────────────────────────────────────────

def _qb_from_overlay(overlay: dict) -> QBClient:
    """Mirror runner.qb_client_from_overlay (can't import the hyphenated
    runner module by name)."""
    op_ref = overlay["secrets"]["qb"]
    return QBClient(secret_ref={
        "op_item": op_ref["op_item"],
        "op_vault": op_ref.get("op_vault"),
        "fields": op_ref["fields"],
    })


def _store():
    return L.PendingStore(PENDING_DB)


def _run_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _audit(event: str, **fields) -> None:
    try:
        L.invoice_lib.audit_append(AUDIT_PATH, {"event": event, "skill": "invoice-draft-and-send", **fields})
    except Exception:
        pass


# ─── inbox resolution ─────────────────────────────────────────────────────

_FROM_RE = re.compile(r"^\s*(?P<name>.*?)\s*<(?P<email>[^>]+)>\s*$")


def _parse_from(header: str) -> tuple[str, str]:
    """'Greg Sample <greg.sample@example.edu>' -> ('Greg Sample',
    'greg.sample@example.edu'). A bare address returns ('', address)."""
    m = _FROM_RE.match(header or "")
    if m:
        return m.group("name").strip().strip('"'), m.group("email").strip().lower()
    addr = (header or "").strip().lower()
    return "", addr


def _messages_to_candidates(messages: list[dict], now_ms: int) -> list[dict]:
    """Pure: map Gmail message dicts (headers From + internalDate) to the
    {from_name, from_email, days_ago} rows candidates_from_threads expects."""
    out = []
    for m in messages:
        headers = ((m.get("payload") or {}).get("headers")) or []
        frm = next((h.get("value") for h in headers if h.get("name", "").lower() == "from"), "")
        name, email = _parse_from(frm)
        if not email:
            continue
        try:
            internal = int(m.get("internalDate", "0"))
        except (TypeError, ValueError):
            internal = 0
        days_ago = max(0, int((now_ms - internal) / 86_400_000)) if internal else 10**9
        out.append({"from_name": name, "from_email": email, "days_ago": days_ago})
    return out


def _gmail_candidates(svc, name: str, *, max_results: int = 20) -> list[dict]:
    """Search the operator's mailbox for people matching <name> and return
    candidate rows. Thin live wrapper around the pure mapper above."""
    q = f"from:{name}"
    resp = svc.users().messages().list(userId="me", q=q, maxResults=max_results).execute()
    refs = resp.get("messages", []) or []
    msgs = []
    for ref in refs:
        try:
            msgs.append(svc.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["From"]).execute())
        except Exception:
            continue
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return _messages_to_candidates(msgs, now_ms)


def resolve_recipient(svc, name: str, qb) -> "L.Resolution":
    """Inbox-first, QBO fallback. Returns invoice_lib.Resolution."""
    try:
        cands = L.candidates_from_threads(name, _gmail_candidates(svc, name))
    except Exception:
        cands = []
    inbox_decision = L.choose_customer(cands)
    qbo: list[dict] = []
    if inbox_decision.action not in ("resolved", "guess"):
        try:
            c = qb.find_customer_by_name(name)
            if c:
                qbo = [{"id": c.get("Id"), "name": c.get("DisplayName"),
                        "email": (c.get("PrimaryEmailAddr") or {}).get("Address")}]
        except Exception:
            pass
    return L.resolve_customer(inbox_decision, qbo)


# ─── turn 1: draft the unsent invoice + ask approval ──────────────────────

def _extract_trigger_line(text: str) -> str:
    """Pull the 'send ... invoice ...' line out of the email text."""
    for line in (text or "").splitlines():
        low = line.lower()
        if "invoice" in low and ("over" in low or "$" in low):
            return line.strip()
    return (text or "").strip().splitlines()[0] if text.strip() else ""


def handle_new_invoice_trigger(
    svc, overlay: dict, msg: dict, subject: str, body: str, *,
    qb=None, send_reply: Optional[Callable] = None,
) -> dict:
    qb = qb or _qb_from_overlay(overlay)
    if send_reply is None:
        from lib.email_runner import send_reply as _sr
        send_reply = _sr
    operator_email = (overlay.get("email") or {}).get("operator_email", "")
    thread_id = msg.get("threadId", msg.get("id"))
    full = f"{subject}\n{body}"

    # parse (fail closed on bad amount / phrasing)
    try:
        parsed = L.parse_trigger(_extract_trigger_line(full))
    except (L.InvalidAmount, L.InvalidTrigger) as e:
        _audit("invoice_parse_failed", thread_id=thread_id, error=str(e))
        send_reply(overlay, msg,
                   "I couldn't read that invoice request. Use: send an invoice "
                   "to <name> over <amount> for <services>. "
                   f"(reason: {e})")
        return {"status": "parse_failed"}

    # resolve who (inbox -> QBO -> fail closed)
    resolution = resolve_recipient(svc, parsed.name, qb)
    if not resolution.proceeds or not resolution.email:
        _audit("invoice_resolution_failed", thread_id=thread_id,
               name=parsed.name, action=resolution.action)
        send_reply(overlay, msg,
                   f"I could not confidently identify who '{parsed.name}' is "
                   f"({resolution.action}); {resolution.reason}. I created "
                   "nothing. Reply with their email or a clearer name.")
        return {"status": "resolution_failed", "action": resolution.action}

    # idempotency (local ledger by trigger_hash)
    store = _store()
    th = L.trigger_hash(resolution.name or parsed.name, parsed.amount,
                        parsed.services, _run_date())
    existing = store.find_by_trigger_hash(th)
    if existing and existing.get("status") in ("pending", "approved", "sent"):
        send_reply(overlay, msg,
                   f"That invoice already exists (status {existing['status']}, "
                   f"id {existing['invoice_id']}); not creating a duplicate.")
        return {"status": "duplicate", "invoice_id": existing["invoice_id"]}

    # customer: existing vs create
    customer_id = resolution.qbo_customer_id
    customer_state = "existing"
    if not customer_id:
        existing_cust = None
        try:
            existing_cust = qb.find_customer_by_name(resolution.name)
        except Exception:
            existing_cust = None
        if existing_cust:
            customer_id = existing_cust.get("Id")
        else:
            created = qb.create_customer(resolution.name, email=resolution.email)
            customer_id = created.get("Id")
            customer_state = "created"

    # service item: must already exist (REST item-create needs an income
    # account id; fail closed rather than guess the revenue mapping)
    item = L.decide_service_item(parsed.services)
    prod = None
    try:
        items = qb.list_items(item.name)
        prod = next((i for i in items
                     if (i.get("Name") or "").strip().lower() == item.name.lower()), None)
        if prod is None and items:
            prod = items[0]
    except Exception:
        prod = None
    if not prod:
        _audit("invoice_item_missing", thread_id=thread_id, item=item.name)
        send_reply(overlay, msg,
                   f"I could not find a service item named '{item.name}' in "
                   "QuickBooks. Create it once in QBO (mapped to your income "
                   "account), then re-send. I created nothing.")
        return {"status": "item_missing"}
    product_id = prod.get("Id")
    income_account = (prod.get("IncomeAccountRef") or {}).get("name")

    # create the UNSENT invoice
    inv_obj = {
        "CustomerRef": {"value": str(customer_id)},
        "BillEmail": {"Address": resolution.email},
        "BillEmailCc": {"Address": CC_ADDRESS},
        "Line": [{
            "DetailType": "SalesItemLineDetail",
            "Amount": float(parsed.amount),
            "Description": parsed.services,
            "SalesItemLineDetail": {
                "ItemRef": {"value": str(product_id)},
                "Qty": 1, "UnitPrice": float(parsed.amount),
            },
        }],
    }
    created_inv = qb.create_invoice(inv_obj)
    invoice_id = str(created_inv.get("Id"))
    doc = created_inv.get("DocNumber") or invoice_id

    # summary on the origin medium (email in-thread)
    facts = L.SummaryFacts(
        trigger_name=parsed.name,
        resolution_action=resolution.action,
        resolution_source=resolution.source or "qbo",
        chosen_name=resolution.name or "", chosen_email=resolution.email or "",
        resolution_reason=resolution.reason, alternatives=[],
        customer_state=customer_state, amount=parsed.amount,
        amount_display=parsed.amount_display, services=parsed.services,
        invoice_id=f"{doc} (id {invoice_id})", invoice_link="",
        cc=CC_ADDRESS, income_account=income_account,
        income_unconfirmed=not bool(income_account),
    )
    summary = L.build_summary(facts)["text"]
    sent_id = send_reply(overlay, msg, summary)

    # durable approval state (email_intents) + idempotency/send-params (PendingStore)
    intent = email_intents.open_intent(
        thread_id=thread_id, operator_email=operator_email,
        trigger_subject=subject, first_text=full,
        intent_kind="invoice",
        initial_status=email_intents.IntentStatus.AWAITING_APPROVAL,
    )
    intent.final_invoice_id = invoice_id
    if sent_id:
        intent.bot_sent_message_ids.append(sent_id)
    intent.processed_operator_message_ids.append(msg.get("id", ""))
    email_intents.save(intent)
    store.record_pending({
        "invoice_id": invoice_id, "trigger_hash": th, "status": "pending",
        "medium": "email", "origin_ref": thread_id, "customer": resolution.name,
        "email": resolution.email, "amount": str(parsed.amount),
        "services": parsed.services, "cc": CC_ADDRESS,
    })
    _audit("invoice_drafted", thread_id=thread_id, invoice_id=invoice_id,
           doc=doc, customer=resolution.name, amount=str(parsed.amount),
           customer_state=customer_state)
    return {"status": "drafted", "invoice_id": invoice_id, "customer_state": customer_state}


# ─── reply: approve -> send, reject -> delete ─────────────────────────────

def handle_invoice_reply(
    svc, overlay: dict, intent, msg: dict, reply_text: str, *,
    qb=None, send_reply: Optional[Callable] = None,
) -> dict:
    qb = qb or _qb_from_overlay(overlay)
    if send_reply is None:
        from lib.email_runner import send_reply as _sr
        send_reply = _sr
    verdict = L.classify_reply(reply_text)
    store = _store()
    invoice_id = getattr(intent, "final_invoice_id", None)
    row = store.load_pending(invoice_id) if invoice_id else None

    if verdict == "approve":
        if not row or not row.get("email"):
            send_reply(overlay, msg,
                       "I can't send this invoice — the recipient email is not "
                       "on file. Please re-send the request.")
            email_intents.set_status(intent, email_intents.IntentStatus.DROPPED)
            return {"status": "send_failed_no_email"}
        if row.get("status") == "sent":
            send_reply(overlay, msg, "This invoice was already sent. Nothing to do.")
            email_intents.set_status(intent, email_intents.IntentStatus.COMPLETED)
            return {"status": "already_sent"}
        store.set_status(invoice_id, "approved")
        cc = row.get("cc") or CC_ADDRESS
        qb.send_invoice(invoice_id, to_email=row["email"], cc_email=cc)
        store.set_status(invoice_id, "sent")
        send_reply(overlay, msg,
                   f"Sent. Invoice {invoice_id} emailed to {row['email']} "
                   f"with {cc} in CC.")
        email_intents.set_status(intent, email_intents.IntentStatus.COMPLETED)
        _audit("invoice_sent", invoice_id=invoice_id, to=row["email"], cc=cc)
        return {"status": "sent", "invoice_id": invoice_id}

    if verdict == "reject":
        try:
            qb.delete_invoice(invoice_id)
        except Exception as e:  # noqa: BLE001
            _audit("invoice_delete_failed", invoice_id=invoice_id, error=str(e))
        if row:
            store.set_status(invoice_id, "discarded")
        send_reply(overlay, msg,
                   "Got it. I deleted the draft invoice and sent nothing.")
        email_intents.set_status(intent, email_intents.IntentStatus.DROPPED)
        _audit("invoice_rejected", invoice_id=invoice_id)
        return {"status": "rejected", "invoice_id": invoice_id}

    # change / unclear -> keep open, ask for explicit yes/no
    send_reply(overlay, msg,
               "To keep the audit log clean I need an explicit reply: APPROVE "
               "to send the invoice, or REJECT to delete the draft. (To change "
               "the amount or recipient, send a fresh invoice request.)")
    return {"status": "awaiting_clarification"}
