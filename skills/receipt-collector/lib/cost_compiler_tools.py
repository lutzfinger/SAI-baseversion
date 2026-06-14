"""Tool surface for the cost-compiler trigger agent.

Each tool is a Python function returning a JSON-serialisable dict. The
agent runner (`lib/cost_compiler_agent.py`) registers them with the
Anthropic SDK's tool-use API.

Two rights tiers (enforced by which functions exist + what they do):

  * **read_only** — QB customer + calendar + overlay lookups; no mutation
  * **propose_only** — stages a plan.json proposal under
    `<overlay>/trip_runs/<slug>/proposed_plan.json`; never writes to QB

Every tool validates its inputs per SAI principle #6a (schema enforcement
at every boundary). Validation failure returns an error dict the agent
can read and correct on the next turn — never silently succeeds.

The full tool surface is declared in `cost_compiler_agent.surface.yaml`.
This file is the implementation; the YAML is the contract.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional


# ─── input-bounds constants ────────────────────────────────────────────

MAX_CUSTOMER_FILTER_LEN: int = 64
MAX_KEYWORD_LEN: int = 64
MAX_CALENDAR_WINDOW_DAYS: int = 90
MAX_QB_CUSTOMERS_RETURNED: int = 50
MAX_CALENDAR_EVENTS_RETURNED: int = 100

TRIP_SLUG_PATTERN = re.compile(r"^[a-z0-9]+-[0-9]{4}-[0-9]{2}$")
ALLOWED_CURRENCIES = {"USD", "EUR", "GBP", "CHF", "CAD", "JPY"}


# ─── per-invocation context ────────────────────────────────────────────

@dataclass
class ToolContext:
    """Per-invocation state every tool reads from."""
    overlay: dict
    qb_client: Any                           # lib.qb_client.QBClient
    operator_text: str = ""
    audit_log: list[dict] = field(default_factory=list)
    # Tracks customer IDs that list_qb_customers returned this turn.
    # propose_plan validates that customer_id was actually seen — the
    # agent can't invent a customer that wasn't surfaced by the tool.
    seen_customer_ids: set[str] = field(default_factory=set)


# ─── read-only tools ───────────────────────────────────────────────────

def list_qb_customers(ctx: ToolContext, contains: str | None = None) -> dict:
    """Return up to 50 QB customers, optionally filtered by substring."""
    if contains is not None:
        if not isinstance(contains, str):
            return {"error": "contains must be a string"}
        if len(contains) > MAX_CUSTOMER_FILTER_LEN:
            return {"error": f"contains exceeds {MAX_CUSTOMER_FILTER_LEN} chars"}

    needle = (contains or "").strip().lower()
    out: list[dict] = []
    try:
        rows = ctx.qb_client.list_customers()
    except AttributeError:
        # list_customers wasn't implemented yet on QBClient — try the
        # narrower find_customer_by_name with the needle.
        if needle:
            try:
                c = ctx.qb_client.find_customer_by_name(needle)
                if c:
                    out.append({
                        "Id": c["Id"],
                        "DisplayName": c["DisplayName"],
                        "currency": (c.get("CurrencyRef") or {}).get("value", "USD"),
                    })
            except Exception as e:
                return {"error": f"QB lookup failed: {e}"}
        else:
            return {"error": "QBClient lacks list_customers(); pass `contains` to use find_customer_by_name."}
    except Exception as e:
        return {"error": f"QB list_customers failed: {e}"}
    else:
        for c in rows:
            name = c.get("DisplayName") or ""
            if needle and needle not in name.lower():
                continue
            out.append({
                "Id": c.get("Id"),
                "DisplayName": name,
                "currency": (c.get("CurrencyRef") or {}).get("value", "USD"),
            })
            if len(out) >= MAX_QB_CUSTOMERS_RETURNED:
                break

    for c in out:
        ctx.seen_customer_ids.add(str(c["Id"]))
    return {"customers": out}


def search_calendar_events(
    ctx: ToolContext,
    start_iso: str,
    end_iso: str,
    keyword: str | None = None,
) -> dict:
    """Read Google Calendar events between start and end (≤90 day span)."""
    try:
        start = date.fromisoformat(start_iso)
        end = date.fromisoformat(end_iso)
    except ValueError as e:
        return {"error": f"date parse: {e}"}
    if end < start:
        return {"error": "end_iso < start_iso"}
    if (end - start).days > MAX_CALENDAR_WINDOW_DAYS:
        return {"error": f"window > {MAX_CALENDAR_WINDOW_DAYS} days"}
    if keyword is not None:
        if not isinstance(keyword, str):
            return {"error": "keyword must be a string"}
        if len(keyword) > MAX_KEYWORD_LEN:
            return {"error": f"keyword exceeds {MAX_KEYWORD_LEN} chars"}

    try:
        from lib import calendar_fetch
        evs = calendar_fetch.list_events(start, end)
    except FileNotFoundError as e:
        return {"error": f"calendar auth missing: {e}"}
    except Exception as e:
        return {"error": f"calendar fetch failed: {e}"}

    needle = (keyword or "").strip().lower()
    out: list[dict] = []
    for ev in evs:
        summary = ev.get("summary") or ""
        if needle and needle not in summary.lower():
            continue
        s = (ev.get("start") or {})
        e = (ev.get("end") or {})
        out.append({
            "id": ev.get("id"),
            "summary": summary[:120],
            "location": (ev.get("location") or "")[:80],
            "start": s.get("date") or s.get("dateTime"),
            "end": e.get("date") or e.get("dateTime"),
        })
        if len(out) >= MAX_CALENDAR_EVENTS_RETURNED:
            break
    return {"events": out}


def list_payment_accounts(ctx: ToolContext) -> dict:
    """List the overlay's known credit-card accounts."""
    rows: list[dict] = []
    for key, acct in (ctx.overlay.get("payment_accounts") or {}).items():
        if not isinstance(acct, dict):
            continue
        rows.append({
            "id": str(acct.get("id", "")),
            "label": acct.get("label", key),
            "last4": acct.get("last4", ""),
        })
    return {"accounts": rows}


def list_expense_accounts(ctx: ToolContext) -> dict:
    """List the overlay's known expense-account categories."""
    rows: list[dict] = []
    for key, acct in (ctx.overlay.get("expense_accounts") or {}).items():
        if not isinstance(acct, dict):
            continue
        rows.append({
            "key": key,
            "id": str(acct.get("id", "")) if acct.get("id") else None,
            "name": acct.get("name", key),
        })
    return {"accounts": rows}


# ─── propose-only tool ─────────────────────────────────────────────────

def propose_plan(
    ctx: ToolContext,
    *,
    trip_slug: str,
    customer_id: str,
    customer_name: str,
    currency: str,
    start_date: str,
    end_date: str,
    scope_categories: list[str] | None = None,
    summary: str = "",
) -> dict:
    """Stage a plan.json under <overlay>/trip_runs/<slug>/proposed_plan.json.

    This tool does NOT write to QB. Its only side effect is the staged
    JSON file. The runner's `await-approval` step gates whether that
    plan then executes.

    Validation per SAI #6a — every field is checked; any failure
    returns an error dict the agent reads on its next turn.
    """
    # ── input validation ─────────────────────────────────────────────
    if not isinstance(trip_slug, str) or not TRIP_SLUG_PATTERN.match(trip_slug):
        return {"error": f"trip_slug must match /^[a-z0-9]+-YYYY-MM$/: got {trip_slug!r}"}
    if not isinstance(customer_id, str) or not customer_id.strip():
        return {"error": "customer_id required"}
    if customer_id not in ctx.seen_customer_ids:
        return {
            "error": (
                f"customer_id={customer_id!r} was not returned by "
                f"list_qb_customers in this invocation. Call "
                f"list_qb_customers first (#6a — propose tools can't "
                f"invent IDs)."
            )
        }
    if not isinstance(customer_name, str) or not customer_name.strip():
        return {"error": "customer_name required"}
    currency = (currency or "").upper()
    if currency not in ALLOWED_CURRENCIES:
        return {"error": f"currency must be one of {sorted(ALLOWED_CURRENCIES)}"}
    try:
        s = date.fromisoformat(start_date)
        e = date.fromisoformat(end_date)
    except ValueError as ex:
        return {"error": f"date parse: {ex}"}
    if e < s:
        return {"error": "end_date < start_date"}
    if (e - s).days > 60:
        return {"error": "window > 60 days — split into separate trips"}
    valid_scopes: set[str] = {
        k for k in (ctx.overlay.get("expense_accounts") or {}).keys()
    }
    scope_categories = scope_categories or []
    if not isinstance(scope_categories, list):
        return {"error": "scope_categories must be a list of strings"}
    for sc in scope_categories:
        if sc not in valid_scopes:
            return {
                "error": (
                    f"scope_categories contains unknown key {sc!r}. "
                    f"Call list_expense_accounts to see valid keys: "
                    f"{sorted(valid_scopes)}"
                )
            }

    # ── write staged plan ────────────────────────────────────────────
    trip_runs_root = ctx.overlay.get("trip_runs_root") or (
        "~/SAI/skills/receipt-collector/trip_runs"
    )
    out_dir = Path(os.path.expanduser(trip_runs_root)) / trip_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    staged = out_dir / "proposed_plan.json"

    payload = {
        "proposal_id": f"plan::{trip_slug}::{int(time.time())}",
        "staged_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trip_slug": trip_slug,
        "customer": {"Id": customer_id, "DisplayName": customer_name},
        "invoice_currency": currency,
        "trip_start": s.isoformat(),
        "trip_end": e.isoformat(),
        "scope_categories": scope_categories,
        "summary": summary[:600],
        "operator_text": ctx.operator_text[:500],
    }
    staged.write_text(json.dumps(payload, indent=2))

    # ── operator-facing summary text ─────────────────────────────────
    scope_part = (
        ", ".join(scope_categories) if scope_categories else "all categories"
    )
    operator_message = (
        f"Plan proposed for *{customer_name}* "
        f"({s.isoformat()}..{e.isoformat()}, {currency}, scope: {scope_part}).\n"
        f"Slug: `{trip_slug}`\n"
        f"Staged: `{staged}`\n"
        f"Summary: {summary[:240]}"
    )

    return {
        "proposal_id": payload["proposal_id"],
        "staged_path": str(staged),
        "operator_message": operator_message,
    }


# ─── tool spec catalog for Anthropic tool_use API ──────────────────────

ANTHROPIC_TOOL_SPECS: list[dict] = [
    {
        "name": "list_qb_customers",
        "description": (
            "Return up to 50 QuickBooks Online customers (Id, DisplayName, "
            "default currency). Use this to identify which customer the "
            "operator's trigger refers to — fuzzy match on DisplayName via "
            "the `contains` argument. ALWAYS call this BEFORE propose_plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contains": {
                    "type": "string",
                    "description": "Optional case-insensitive substring filter (max 64 chars).",
                    "maxLength": MAX_CUSTOMER_FILTER_LEN,
                },
            },
        },
    },
    {
        "name": "search_calendar_events",
        "description": (
            "Read Google Calendar events between start_iso and end_iso "
            "(max 90-day window). Use to confirm or refine a trip window "
            "when the operator only hinted at a month — the calendar will "
            "show the actual travel block. Returns up to 100 events."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_iso": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_iso": {"type": "string", "description": "End date YYYY-MM-DD (within 90 days of start)"},
                "keyword": {
                    "type": "string",
                    "description": "Optional case-insensitive summary filter (max 64 chars).",
                    "maxLength": MAX_KEYWORD_LEN,
                },
            },
            "required": ["start_iso", "end_iso"],
        },
    },
    {
        "name": "list_payment_accounts",
        "description": (
            "List the overlay's known credit-card accounts (id, label, "
            "last4). Use this to understand which cards the operator "
            "expects to be scanned."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_expense_accounts",
        "description": (
            "List the overlay's expense-account categories (e.g., airfare, "
            "hotels, taxis_rideshare). Use this to validate any "
            "scope_categories you pass to propose_plan."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "propose_plan",
        "description": (
            "Stage a plan for the operator's approval. Writes a JSON file "
            "to <overlay>/trip_runs/<trip_slug>/proposed_plan.json. NO QB "
            "writes. Call this ONCE when you have: matched customer "
            "(customer_id from list_qb_customers), confirmed window "
            "(start_date/end_date), currency, and any scope restrictions. "
            "If you can't determine all of these — DON'T call propose_plan; "
            "respond with a clarification question instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "trip_slug": {
                    "type": "string",
                    "description": "Lowercase <customer>-<YYYY>-<MM>, e.g. 'insead-2026-05'.",
                },
                "customer_id": {
                    "type": "string",
                    "description": "QB Customer.Id (must come from list_qb_customers).",
                },
                "customer_name": {
                    "type": "string",
                    "description": "QB Customer.DisplayName.",
                },
                "currency": {
                    "type": "string",
                    "enum": sorted(ALLOWED_CURRENCIES),
                    "description": "ISO 4217 currency code for the customer invoice.",
                },
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD (>= start_date, within 60 days)"},
                "scope_categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of expense-account category keys "
                        "(from list_expense_accounts). Empty list = all "
                        "categories billable."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "One-paragraph rationale shown to the operator.",
                },
            },
            "required": [
                "trip_slug", "customer_id", "customer_name", "currency",
                "start_date", "end_date",
            ],
        },
    },
]


def build_tool_dispatch(ctx: ToolContext) -> dict[str, Callable[..., dict]]:
    """Return a name → callable map for the Anthropic tool_use loop."""
    return {
        "list_qb_customers": lambda **kw: list_qb_customers(ctx, **kw),
        "search_calendar_events": lambda **kw: search_calendar_events(ctx, **kw),
        "list_payment_accounts": lambda **kw: list_payment_accounts(ctx, **kw),
        "list_expense_accounts": lambda **kw: list_expense_accounts(ctx, **kw),
        "propose_plan": lambda **kw: propose_plan(ctx, **kw),
    }
