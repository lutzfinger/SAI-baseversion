"""Unit tests for the QBClient methods added for the invoice-draft flow:
create_customer, get_invoice, send_invoice (CC-then-send), delete_invoice.
All HTTP is mocked — no live QuickBooks."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib import qb_client as qbmod  # noqa: E402


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)
        self.headers = {}

    def json(self):
        return self._payload


@pytest.fixture
def qb(monkeypatch):
    calls = []

    def fake_post(url, **kw):  # token refresh only
        return FakeResp(200, {"access_token": "AT", "refresh_token": "r"})

    def fake_request(method, url, **kw):
        calls.append({"method": method, "url": url,
                      "params": kw.get("params"), "data": kw.get("data")})
        params = kw.get("params") or {}
        if url.endswith("/send"):
            return FakeResp(200, {"Invoice": {"Id": "2307"}})
        if params.get("operation") == "delete":
            return FakeResp(200, {"Invoice": {"status": "Deleted"}})
        if "/invoice/" in url and method == "GET":
            return FakeResp(200, {"Invoice": {"Id": "2307", "SyncToken": "3"}})
        if url.endswith("/customer"):
            return FakeResp(200, {"Customer": {"Id": "C1",
                                  "PrimaryEmailAddr": {"Address": "lablanc@berkeley.edu"}}})
        if url.endswith("/invoice"):
            return FakeResp(200, {"Invoice": {"Id": "2307", "SyncToken": "4"}})
        return FakeResp(200, {})

    fake_requests = type("R", (), {"post": staticmethod(fake_post),
                                   "request": staticmethod(fake_request)})
    monkeypatch.setattr(qbmod, "requests", fake_requests)
    client = qbmod.QBClient(creds={
        "client_id": "i", "client_secret": "s", "refresh_token": "r",
        "realm_id": "R", "environment": "production",
    })
    client._calls = calls
    return client


def test_create_customer(qb):
    out = qb.create_customer("Gregory P. La Blanc", email="lablanc@berkeley.edu")
    assert out["Id"] == "C1"
    post = [c for c in qb._calls if c["url"].endswith("/customer")][-1]
    body = json.loads(post["data"])
    assert body["DisplayName"] == "Gregory P. La Blanc"
    assert body["PrimaryEmailAddr"]["Address"] == "lablanc@berkeley.edu"


def test_send_invoice_sets_cc_then_sends(qb):
    qb.send_invoice("2307", "lablanc@berkeley.edu", cc_email="hello@lutzfinger.com")
    urls = [c["url"] for c in qb._calls]
    # GET (SyncToken) -> POST /invoice (update CC) -> POST /invoice/2307/send
    get_idx = next(i for i, c in enumerate(qb._calls)
                   if c["method"] == "GET" and "/invoice/2307" in c["url"])
    upd_idx = next(i for i, c in enumerate(qb._calls)
                   if c["method"] == "POST" and c["url"].endswith("/invoice"))
    send_idx = next(i for i, c in enumerate(qb._calls) if c["url"].endswith("/send"))
    assert get_idx < upd_idx < send_idx, "must fetch + set CC BEFORE sending"
    upd_body = json.loads(qb._calls[upd_idx]["data"])
    assert upd_body["BillEmailCc"]["Address"] == "hello@lutzfinger.com"
    send_params = qb._calls[send_idx]["params"]
    assert send_params.get("sendTo") == "lablanc@berkeley.edu"


def test_delete_invoice(qb):
    qb.delete_invoice("2307")
    deletes = [c for c in qb._calls
               if (c["params"] or {}).get("operation") == "delete"]
    assert deletes, "delete must POST ?operation=delete"
    body = json.loads(deletes[-1]["data"])
    assert body["Id"] == "2307" and body["SyncToken"] == "3"
