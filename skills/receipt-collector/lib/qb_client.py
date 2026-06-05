"""
QuickBooks Online REST client (atomic, base skill).

All credentials are read from 1Password at construction time. The
caller passes a `creds` dict OR a `secret_ref` dict that names the
1Password item/fields to read.

Public API (atomic):
    QBClient(creds | secret_ref).
    .query(qbo_sql)                       - raw QBO SQL query
    .find_customer_by_name(name)
    .find_vendor_by_name(name)
    .list_accounts(classification=None)
    .list_purchases(start, end)
    .list_purchases_for_account(acc_id, start, end)
    .find_purchase_by_marker(marker, start, end)
    .find_invoice_by_marker(marker, start, end)
    .create_purchase(obj)
    .create_invoice(obj)
    .update_purchase(obj)
    .update_invoice(obj)
    .get_transaction_list(start, end, account=None)

Token refresh is automatic on 401. New refresh tokens are written back
to 1Password (via op_secrets.set_field) so the credential vault stays
authoritative.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import date
from typing import Any, Optional

import requests

from . import op_secrets
from . import op_env

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
API_BASE = {
    "production": "https://quickbooks.api.intuit.com",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
}

# Per SAI #7a: avoid invoking `op` from the daemon. Cache QB OAuth
# creds in macOS Keychain on first construction (one-time `op` call,
# acceptable when operator runs `runner cache-secrets`). Subsequent
# constructions read from Keychain via `security`, no `op` involved.
_QB_KEYCHAIN_FIELDS = (
    "client_id", "client_secret", "refresh_token",
    "realm_id", "environment", "redirect_uri",
)


def _load_qb_creds_from_keychain_or_op(secret_ref: dict) -> dict:
    """Try Keychain first (no `op`, no TCC prompts). Fall back to
    `op_secrets.get_all_fields` if any field is missing — that's a
    one-shot `op` invocation; the caller (cache-secrets) should have
    pre-populated Keychain so this fallback never fires on a healthy
    deployment.
    """
    cached = {}
    missing = []
    for f in _QB_KEYCHAIN_FIELDS:
        v = op_env.get_cached_secret(f"qb-{f}")
        if v:
            cached[f] = v
        else:
            missing.append(f)
    if not missing:
        return cached
    # Fall back to op — but only for the missing fields. Then cache
    # them for next time so the daemon doesn't keep invoking op.
    field_map = secret_ref["fields"]
    raw = op_secrets.get_all_fields(
        secret_ref["op_item"],
        list(field_map.values()),
        vault=secret_ref.get("op_vault"),
    )
    creds = {k: raw[v] for k, v in field_map.items()}
    # Cache everything we just read
    for f in _QB_KEYCHAIN_FIELDS:
        if f in creds and creds[f]:
            try:
                op_env.cache_secret(f"qb-{f}", creds[f])
            except Exception:
                pass
    return creds


def _save_refresh_token_to_keychain(new_refresh: str) -> None:
    """Update the cached refresh_token after QB rotates it."""
    try:
        op_env.cache_secret("qb-refresh_token", new_refresh)
    except Exception:
        pass


class QBClient:
    """QuickBooks Online REST client.

    Two construction forms:

      # Form 1: credentials already loaded (e.g., from overlay code that
      # already read 1Password):
      QBClient(creds={
          "client_id": "...", "client_secret": "...",
          "refresh_token": "...", "realm_id": "...",
          "environment": "production",
      })

      # Form 2: tell the client which 1Password item holds the credentials
      # and let it read at construction time:
      QBClient(secret_ref={
          "op_item": "<your-1password-item-name>",
          "op_vault": "Private",       # optional
          "fields": {
              "client_id":     "client_id",
              "client_secret": "client_secret",
              "refresh_token": "refresh_token",
              "realm_id":      "realm_id",
              "environment":   "environment",   # value should be "production" or "sandbox"
          },
      })
    """

    def __init__(
        self,
        creds: Optional[dict] = None,
        secret_ref: Optional[dict] = None,
    ):
        if creds and secret_ref:
            raise ValueError("Pass either creds OR secret_ref, not both.")
        if not creds and not secret_ref:
            raise ValueError("Pass creds or secret_ref.")
        if secret_ref:
            self._secret_ref = secret_ref
            field_map = secret_ref["fields"]
            # Cache-first: try macOS Keychain (`security`, no prompt).
            # Fall back to `op` only if cache miss — that's a one-time
            # `op` invocation; on next call the Keychain cache hits.
            # Per SAI #7a: daemons should never invoke `op` at all once
            # secrets are cached.
            creds = _load_qb_creds_from_keychain_or_op(secret_ref)
        else:
            self._secret_ref = None
        self.cfg = dict(creds)
        self.base = API_BASE[self.cfg.get("environment", "production")]
        self.realm = self.cfg["realm_id"]
        # Access token is short-lived; obtain a fresh one immediately.
        self._access_token: Optional[str] = None
        self.last_intuit_tid: Optional[str] = None
        self._refresh()

    # ---------------- OAuth ----------------
    def _refresh(self) -> None:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.cfg["refresh_token"],
        }
        resp = requests.post(
            TOKEN_URL,
            data=data,
            auth=(self.cfg["client_id"], self.cfg["client_secret"]),
            headers={"Accept": "application/json"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"QB token refresh failed: HTTP {resp.status_code}\n{resp.text}")
        new = resp.json()
        self._access_token = new["access_token"]
        new_refresh = new["refresh_token"]
        if new_refresh != self.cfg["refresh_token"]:
            self.cfg["refresh_token"] = new_refresh
            if self._secret_ref:
                # Write the rotated refresh_token to BOTH Keychain
                # (active store; read by next QBClient construction)
                # AND 1Password (durable backup). Per SAI #7a we
                # prefer Keychain for the daemon's reads — `op` is
                # invoked here only because rotation is rare and
                # writing back to 1P keeps the credential vault as
                # source-of-truth.
                _save_refresh_token_to_keychain(new_refresh)
                try:
                    op_secrets.set_field(
                        self._secret_ref["op_item"],
                        self._secret_ref["fields"]["refresh_token"],
                        new_refresh,
                        vault=self._secret_ref.get("op_vault"),
                    )
                except Exception:
                    # Failing the 1P writeback is non-fatal; Keychain
                    # has the new token and the daemon will use it.
                    pass
        self._token_obtained_at = time.time()

    # ---------------- HTTP ----------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base}{path}"
        # Merge so caller-supplied headers (e.g., a multipart Content-Type)
        # override our JSON defaults.
        merged = self._headers()
        merged.update(kwargs.get("headers") or {})
        kwargs["headers"] = merged
        resp = requests.request(method, url, timeout=60, **kwargs)
        if resp.status_code == 401:
            self._refresh()
            merged = self._headers()
            merged.update({k: v for k, v in kwargs["headers"].items()
                           if k.lower() not in ("authorization",)})
            kwargs["headers"] = merged
            resp = requests.request(method, url, timeout=60, **kwargs)
        self.last_intuit_tid = resp.headers.get("intuit_tid")
        if resp.status_code >= 400:
            print(f"[qb_client] HTTP {resp.status_code} {method} {path}  intuit_tid={self.last_intuit_tid}")
        return resp

    # ---------------- Query ----------------
    def query(self, qbo_sql: str) -> dict[str, Any]:
        resp = self._request("GET", f"/v3/company/{self.realm}/query",
                              params={"query": qbo_sql, "minorversion": "75"})
        if resp.status_code != 200:
            raise RuntimeError(f"QB query failed: {resp.status_code}\n{resp.text}")
        return resp.json()

    # --- Customer / Vendor / Account / Item ---
    def find_customer_by_name(self, name: str) -> Optional[dict]:
        safe = name.replace("'", "''")
        rows = self.query(
            f"SELECT * FROM Customer WHERE DisplayName LIKE '%{safe}%' MAXRESULTS 50"
        ).get("QueryResponse", {}).get("Customer", [])
        if not rows:
            return None
        for r in rows:
            if (r.get("DisplayName") or "").strip().lower() == name.strip().lower():
                return r
        return rows[0]

    def list_customers(self, max_results: int = 200) -> list[dict]:
        """List up to `max_results` Customer rows (Id, DisplayName, CurrencyRef, …).

        Used by the cost-compiler agent's `list_qb_customers` tool so
        Haiku can do fuzzy customer matching across the whole roster
        rather than only the operator's literal phrasing.
        """
        sql = f"SELECT * FROM Customer MAXRESULTS {int(max_results)}"
        return self.query(sql).get("QueryResponse", {}).get("Customer", [])

    def find_vendor_by_name(self, name: str) -> Optional[dict]:
        safe = name.replace("'", "''")
        rows = self.query(
            f"SELECT * FROM Vendor WHERE DisplayName LIKE '%{safe}%' MAXRESULTS 50"
        ).get("QueryResponse", {}).get("Vendor", [])
        return rows[0] if rows else None

    def list_accounts(self, classification: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM Account"
        if classification:
            sql += f" WHERE Classification = '{classification}'"
        sql += " MAXRESULTS 500"
        return self.query(sql).get("QueryResponse", {}).get("Account", [])

    def list_items(self, name_filter: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM Item"
        if name_filter:
            safe = name_filter.replace("'", "''")
            sql += f" WHERE Name LIKE '%{safe}%'"
        sql += " MAXRESULTS 100"
        return self.query(sql).get("QueryResponse", {}).get("Item", [])

    # --- Purchase / Bill / Invoice ---
    def list_purchases(self, start: date, end: date) -> list[dict]:
        sql = (
            "SELECT * FROM Purchase "
            f"WHERE TxnDate >= '{start.isoformat()}' AND TxnDate <= '{end.isoformat()}' "
            "MAXRESULTS 500"
        )
        return self.query(sql).get("QueryResponse", {}).get("Purchase", [])

    def list_purchases_for_account(self, account_id: str, start: date, end: date) -> list[dict]:
        rows = self.list_purchases(start, end)
        return [p for p in rows if (p.get("AccountRef") or {}).get("value") == account_id]

    def find_purchase_by_marker(self, marker: str, start: date, end: date) -> Optional[dict]:
        for p in self.list_purchases(start, end):
            if marker in (p.get("PrivateNote") or ""):
                return p
        return None

    def find_invoice_by_marker(self, marker: str, start: date, end: date) -> Optional[dict]:
        sql = (
            "SELECT * FROM Invoice "
            f"WHERE TxnDate >= '{start.isoformat()}' AND TxnDate <= '{end.isoformat()}' "
            "MAXRESULTS 500"
        )
        for inv in self.query(sql).get("QueryResponse", {}).get("Invoice", []):
            memo = (inv.get("CustomerMemo") or {}).get("value") or ""
            if marker in (inv.get("PrivateNote") or "") or marker in memo:
                return inv
        return None

    def get_transaction_list(
        self, start: date, end: date, account_id: Optional[str] = None
    ) -> dict[str, Any]:
        """Reports API. NOTE: as of minorversion 75, the `account` param often
        does not filter — callers should still post-filter the returned rows
        by account_name."""
        params = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "minorversion": "75",
        }
        if account_id:
            params["account"] = account_id
        resp = self._request(
            "GET", f"/v3/company/{self.realm}/reports/TransactionList", params=params
        )
        if resp.status_code != 200:
            raise RuntimeError(f"QB TransactionList failed: {resp.status_code}\n{resp.text}")
        return resp.json()

    # --- Writes ---
    def create_purchase(self, obj: dict) -> dict:
        resp = self._request(
            "POST", f"/v3/company/{self.realm}/purchase",
            params={"minorversion": "75"}, data=json.dumps(obj),
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"QB create Purchase failed: {resp.status_code}\n{resp.text}")
        return resp.json().get("Purchase") or resp.json()

    def update_purchase(self, obj: dict) -> dict:
        body = {**obj, "sparse": True}
        resp = self._request(
            "POST", f"/v3/company/{self.realm}/purchase",
            params={"minorversion": "75"}, data=json.dumps(body),
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"QB update Purchase failed: {resp.status_code}\n{resp.text}")
        return resp.json().get("Purchase") or resp.json()

    def create_invoice(self, obj: dict) -> dict:
        resp = self._request(
            "POST", f"/v3/company/{self.realm}/invoice",
            params={"minorversion": "75"}, data=json.dumps(obj),
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"QB create Invoice failed: {resp.status_code}\n{resp.text}")
        return resp.json().get("Invoice") or resp.json()

    def update_invoice(self, obj: dict) -> dict:
        body = {**obj, "sparse": True}
        resp = self._request(
            "POST", f"/v3/company/{self.realm}/invoice",
            params={"minorversion": "75"}, data=json.dumps(body),
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"QB update Invoice failed: {resp.status_code}\n{resp.text}")
        return resp.json().get("Invoice") or resp.json()

    # --- Customer create (for the headless invoice-draft flow) ---
    def create_customer(
        self, display_name: str, email: Optional[str] = None,
        first: Optional[str] = None, last: Optional[str] = None,
    ) -> dict:
        """Create a QBO Customer. Used by the invoice-draft email flow when
        the resolved person is not yet a customer."""
        obj: dict[str, Any] = {"DisplayName": display_name}
        if email:
            obj["PrimaryEmailAddr"] = {"Address": email}
        if first:
            obj["GivenName"] = first
        if last:
            obj["FamilyName"] = last
        resp = self._request(
            "POST", f"/v3/company/{self.realm}/customer",
            params={"minorversion": "75"}, data=json.dumps(obj),
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"QB create Customer failed: {resp.status_code}\n{resp.text}")
        return resp.json().get("Customer") or resp.json()

    def get_invoice(self, invoice_id: str) -> dict:
        resp = self._request(
            "GET", f"/v3/company/{self.realm}/invoice/{invoice_id}",
            params={"minorversion": "75"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"QB get Invoice failed: {resp.status_code}\n{resp.text}")
        return resp.json().get("Invoice") or resp.json()

    def send_invoice(
        self, invoice_id: str, to_email: str, cc_email: Optional[str] = None,
    ) -> dict:
        """Email an existing (unsent) invoice via QBO SendInvoice.

        CC is NOT a send-param in QBO: it must live on the invoice's
        BillEmailCc. So this is a 3-call sequence — fetch (SyncToken),
        sparse-update BillEmail (+ BillEmailCc), then POST /send?sendTo=.
        The send happens ONLY here, called from the approve path; never on
        draft creation (PRINCIPLES #2/#7a — gated by the operator's reply).
        """
        inv = self.get_invoice(invoice_id)
        sync = str(inv.get("SyncToken", "0"))
        update: dict[str, Any] = {
            "Id": str(invoice_id), "SyncToken": sync,
            "BillEmail": {"Address": to_email},
        }
        if cc_email:
            update["BillEmailCc"] = {"Address": cc_email}
        self.update_invoice(update)
        resp = self._request(
            "POST", f"/v3/company/{self.realm}/invoice/{invoice_id}/send",
            params={"minorversion": "75", "sendTo": to_email},
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"QB send Invoice failed: {resp.status_code}\n{resp.text}")
        return resp.json().get("Invoice") or resp.json()

    def delete_invoice(self, invoice_id: str, sync_token: Optional[str] = None) -> dict:
        """Hard-delete an unsent invoice (the reject path). Fetches the
        SyncToken first if not supplied."""
        if sync_token is None:
            sync_token = str(self.get_invoice(invoice_id).get("SyncToken", "0"))
        body = {"Id": str(invoice_id), "SyncToken": str(sync_token)}
        resp = self._request(
            "POST", f"/v3/company/{self.realm}/invoice",
            params={"minorversion": "75", "operation": "delete"},
            data=json.dumps(body),
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"QB delete Invoice failed: {resp.status_code}\n{resp.text}")
        return resp.json()
