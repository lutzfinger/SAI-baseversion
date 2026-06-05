"""invoice_intent handler: turn-1 draft, approve->send, reject->delete,
ambiguous, e2e round-trip, and the _route_reply wiring. Fakes for QBClient +
Gmail + send_reply; state paths redirected to tmp."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib import email_intents  # noqa: E402
from lib import invoice_intent  # noqa: E402
from lib import invoice_logic_bridge as L  # noqa: E402


# ── fakes ─────────────────────────────────────────────────────────────────

class FakeQB:
    def __init__(self, *, customer=None, items=None):
        self._customer = customer        # find_customer_by_name return
        self._items = items if items is not None else "echo"
        self.calls = []

    def find_customer_by_name(self, name):
        self.calls.append(("find_customer", name))
        return self._customer

    def create_customer(self, display_name, email=None, **kw):
        self.calls.append(("create_customer", display_name, email))
        return {"Id": "C-NEW", "PrimaryEmailAddr": {"Address": email}}

    def list_items(self, name=None):
        self.calls.append(("list_items", name))
        if self._items == "echo":
            return [{"Id": "18", "Name": name,
                     "IncomeAccountRef": {"name": "Speaking Income"}}]
        return self._items

    def create_invoice(self, obj):
        self.calls.append(("create_invoice", obj))
        return {"Id": "2307", "DocNumber": "0000040"}

    def send_invoice(self, invoice_id, to_email, cc_email=None):
        self.calls.append(("send_invoice", invoice_id, to_email, cc_email))
        return {"Id": invoice_id}

    def delete_invoice(self, invoice_id, sync_token=None):
        self.calls.append(("delete_invoice", invoice_id))
        return {"status": "Deleted"}

    def names(self):
        return [c[0] for c in self.calls]


class _Exec:
    def __init__(self, v):
        self._v = v

    def execute(self):
        return self._v


class _Msgs:
    def __init__(self, msgs):
        self._by_id = {m["id"]: m for m in msgs}
        self._ids = [m["id"] for m in msgs]

    def list(self, **kw):
        return _Exec({"messages": [{"id": i} for i in self._ids]})

    def get(self, **kw):
        return _Exec(self._by_id[kw["id"]])


class _Users:
    def __init__(self, msgs):
        self._m = _Msgs(msgs)

    def messages(self):
        return self._m


class FakeGmail:
    def __init__(self, msgs):
        self._u = _Users(msgs)

    def users(self):
        return self._u


def _greg_inbox():
    return FakeGmail([{
        "id": "g1", "internalDate": "1780000000000",
        "payload": {"headers": [
            {"name": "From", "value": "Gregory P La Blanc <lablanc@berkeley.edu>"}]},
    }])


@pytest.fixture
def tmp_state(monkeypatch, tmp_path):
    monkeypatch.setattr(email_intents, "_state_root",
                        lambda: tmp_path / "intents")
    monkeypatch.setattr(invoice_intent, "PENDING_DB", str(tmp_path / "pending.db"))
    monkeypatch.setattr(invoice_intent, "AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    return tmp_path


def _capture_send():
    sent = []

    def send_reply(overlay, msg, body, attachments=None):
        sent.append(body)
        return "bot-msg-id"
    return sent, send_reply


# ── turn 1 ────────────────────────────────────────────────────────────────

def test_turn1_drafts_unsent_and_asks_approval(tmp_state):
    qb = FakeQB(customer=None)  # not a customer yet -> create
    sent, send_reply = _capture_send()
    out = invoice_intent.handle_new_invoice_trigger(
        _greg_inbox(), {"email": {"operator_email": "hello@lutzfinger.com"}},
        {"id": "m1", "threadId": "t1"},
        "send an invoice to Gregory over 1500 for keynote", "",
        qb=qb, send_reply=send_reply)
    assert out["status"] == "drafted" and out["invoice_id"] == "2307"
    assert qb.names().count("create_customer") == 1
    assert qb.names().count("create_invoice") == 1
    assert "send_invoice" not in qb.names()       # NOT sent at draft time
    assert sent and "APPROVE" in sent[-1] and "REJECT" in sent[-1]
    intent = email_intents.load("t1")
    assert intent and intent.intent_kind == "invoice"
    assert intent.status == email_intents.IntentStatus.AWAITING_APPROVAL
    assert intent.final_invoice_id == "2307"


def test_turn1_bad_amount_fails_closed(tmp_state):
    qb = FakeQB(customer=None)
    sent, send_reply = _capture_send()
    out = invoice_intent.handle_new_invoice_trigger(
        _greg_inbox(), {"email": {"operator_email": "hello@lutzfinger.com"}},
        {"id": "m1", "threadId": "t1"},
        "send gregory an invoice over 5k", "", qb=qb, send_reply=send_reply)
    assert out["status"] == "parse_failed"
    assert "create_invoice" not in qb.names()
    assert email_intents.load("t1") is None


def test_turn1_missing_item_fails_closed(tmp_state):
    qb = FakeQB(customer=None, items=[])   # no service item found
    sent, send_reply = _capture_send()
    out = invoice_intent.handle_new_invoice_trigger(
        _greg_inbox(), {"email": {"operator_email": "hello@lutzfinger.com"}},
        {"id": "m1", "threadId": "t1"},
        "send an invoice to Gregory over 1500 for skydiving", "",
        qb=qb, send_reply=send_reply)
    assert out["status"] == "item_missing"
    assert "create_invoice" not in qb.names()


# ── replies ────────────────────────────────────────────────────────────────

def _open_awaiting(tmp_path):
    intent = email_intents.open_intent(
        thread_id="t1", operator_email="hello@lutzfinger.com",
        trigger_subject="inv", first_text="send an invoice to Gregory over 1500",
        intent_kind="invoice",
        initial_status=email_intents.IntentStatus.AWAITING_APPROVAL)
    intent.final_invoice_id = "2307"
    email_intents.save(intent)
    store = L.PendingStore(str(tmp_path / "pending.db"))
    store.record_pending({"invoice_id": "2307", "trigger_hash": "h",
                          "status": "pending", "medium": "email",
                          "customer": "Gregory P. La Blanc",
                          "email": "lablanc@berkeley.edu", "amount": "1500",
                          "services": "keynote", "cc": "hello@lutzfinger.com"})
    return intent


def test_reply_approve_sends_with_cc(tmp_state):
    qb = FakeQB()
    sent, send_reply = _capture_send()
    intent = _open_awaiting(tmp_state)
    out = invoice_intent.handle_invoice_reply(
        None, {"email": {}}, intent, {"id": "r1", "threadId": "t1"},
        "approved, send it", qb=qb, send_reply=send_reply)
    assert out["status"] == "sent"
    sends = [c for c in qb.calls if c[0] == "send_invoice"]
    assert sends and sends[0][1] == "2307" and sends[0][2] == "lablanc@berkeley.edu"
    assert sends[0][3] == "hello@lutzfinger.com"   # CC
    assert email_intents.load("t1").status == email_intents.IntentStatus.COMPLETED


def test_reply_reject_deletes(tmp_state):
    qb = FakeQB()
    sent, send_reply = _capture_send()
    intent = _open_awaiting(tmp_state)
    out = invoice_intent.handle_invoice_reply(
        None, {"email": {}}, intent, {"id": "r1", "threadId": "t1"},
        "no, cancel that", qb=qb, send_reply=send_reply)
    assert out["status"] == "rejected"
    assert "delete_invoice" in qb.names()
    assert "send_invoice" not in qb.names()
    assert email_intents.load("t1").status == email_intents.IntentStatus.DROPPED


def test_reply_ambiguous_keeps_open(tmp_state):
    qb = FakeQB()
    sent, send_reply = _capture_send()
    intent = _open_awaiting(tmp_state)
    out = invoice_intent.handle_invoice_reply(
        None, {"email": {}}, intent, {"id": "r1", "threadId": "t1"},
        "hmm not sure yet", qb=qb, send_reply=send_reply)
    assert out["status"] == "awaiting_clarification"
    assert "send_invoice" not in qb.names() and "delete_invoice" not in qb.names()
    assert email_intents.load("t1").status == email_intents.IntentStatus.AWAITING_APPROVAL


# ── e2e + wiring ────────────────────────────────────────────────────────────

def test_e2e_email_roundtrip(tmp_state):
    qb = FakeQB(customer=None)
    sent, send_reply = _capture_send()
    invoice_intent.handle_new_invoice_trigger(
        _greg_inbox(), {"email": {"operator_email": "hello@lutzfinger.com"}},
        {"id": "m1", "threadId": "t1"},
        "send an invoice to Gregory over 1500 for keynote", "",
        qb=qb, send_reply=send_reply)
    intent = email_intents.load("t1")
    out = invoice_intent.handle_invoice_reply(
        None, {"email": {}}, intent, {"id": "r1", "threadId": "t1"},
        "approve", qb=qb, send_reply=send_reply)
    assert out["status"] == "sent"
    assert qb.names().count("create_invoice") == 1
    assert qb.names().count("send_invoice") == 1
    assert email_intents.load("t1").status == email_intents.IntentStatus.COMPLETED


def test_route_reply_wires_invoice_intent(tmp_state, monkeypatch):
    # email_runner._route_reply must dispatch an "invoice" intent to
    # invoice_intent.handle_invoice_reply (and not the cost_compiler path).
    from lib import email_runner
    called = {}
    monkeypatch.setattr(invoice_intent, "handle_invoice_reply",
                        lambda *a, **k: called.setdefault("hit", True))
    intent = email_intents.open_intent(
        thread_id="t9", operator_email="hello@lutzfinger.com",
        trigger_subject="inv", first_text="x", intent_kind="invoice",
        initial_status=email_intents.IntentStatus.AWAITING_APPROVAL)
    email_runner._route_reply(None, {"email": {}}, intent,
                              {"id": "r1", "threadId": "t9"}, "approve")
    assert called.get("hit") is True
