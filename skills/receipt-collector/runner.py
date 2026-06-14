#!/usr/bin/env python3
"""
receipt-collector — atomic-step CLI entry point (base skill).

Every atomic tier in skill.yaml is exposed as a subcommand so it can be
invoked independently for testing, partial-pipeline runs, and the cleanup
mode. The overlay's `config/identity.yaml` supplies all personal IDs;
the base skill carries no defaults that would tie it to a single user.

Usage:
  python -m skills.receipt-collector.runner check-auth
  python -m skills.receipt-collector.runner infer-window  --hint "<customer> <month>" --search-start 2026-05-01 --search-end 2026-05-31
  python -m skills.receipt-collector.runner scan-cards    --start 2026-05-05 --end 2026-05-18
  python -m skills.receipt-collector.runner search-receipts --start 2026-05-05 --end 2026-05-18
  python -m skills.receipt-collector.runner create-purchases  --trip insead-2026-05 --plan plan.json
  python -m skills.receipt-collector.runner create-invoice    --trip insead-2026-05 --plan plan.json
  python -m skills.receipt-collector.runner cleanup-pass      --rules ~/SAI/skills/receipt-collector/bookkeeping-rules.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import yaml

# package-relative imports
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from lib.qb_client import QBClient
from lib.gmail_search import build_receipt_query
from lib.log import log_event
from lib import purchases as purchases_lib
from lib import invoices as invoices_lib
from lib import qb_tags
from lib import receipt_match
from lib import forwarded_receipts
from lib import google_photos
from lib import vision_extract
from lib import llm_costs
from lib import fx_live
from lib import sense_check
from lib import parse_trigger
from lib import cost_compiler_agent
from lib import reconcile as reconcile_lib
from lib import approval as approval_lib
from lib import trip_calendar
from lib import calendar_fetch
from lib import cleanup as cleanup_lib
# Surface runners are imported lazily inside cmd_* (they require
# optional deps: slack-sdk for slack_runner).


def load_overlay(overlay_root: str) -> dict:
    """Load operator overlay config. Looks for config/identity.yaml first
    (preferred new name) then falls back to config/<owner>.yaml."""
    root = Path(os.path.expanduser(overlay_root))
    for fname in ("config/identity.yaml", "config/lutz.yaml"):
        p = root / fname
        if p.exists():
            cfg = yaml.safe_load(p.read_text())
            cfg["_overlay_root"] = str(root)
            return cfg
    raise SystemExit(f"No overlay config at {root}/config/identity.yaml (or lutz.yaml)")


def qb_client_from_overlay(overlay: dict) -> QBClient:
    op_ref = overlay["secrets"]["qb"]
    return QBClient(secret_ref={
        "op_item": op_ref["op_item"],
        "op_vault": op_ref.get("op_vault"),
        "fields": op_ref["fields"],
    })


# ---------------- subcommands ----------------

def cmd_check_auth(args, overlay: dict) -> int:
    client = qb_client_from_overlay(overlay)
    resp = client._request("GET", f"/v3/company/{client.realm}/companyinfo/{client.realm}")
    info = resp.json().get("CompanyInfo", {}) if resp.status_code == 200 else {}
    print(f"QB: connected to {info.get('CompanyName')!r}  realm={client.realm}")
    log_event("check_auth", {"company": info.get("CompanyName"), "realm": client.realm})
    return 0


def cmd_slack_listen(args, overlay: dict) -> int:
    """Block forever, listening to the configured Slack channel. Each
    operator message triggers a full cost-compiler plan; status updates
    post back to the same thread (per surface-continuity invariant)."""
    try:
        from lib import slack_runner
    except ImportError as e:
        print(str(e), file=sys.stderr)
        return 4
    if not (overlay.get("slack") or {}).get("channel_id"):
        print("Overlay is missing slack.channel_id. Add to identity.yaml:", file=sys.stderr)
        print("""
  slack:
    bot_token_op_ref:
      op_item: "<your slack bot 1Password item>"
      op_vault: "<vault>"
      field: "credential"
    channel_id: "C0XXXXXXXX"
    operator_user_id: "U0XXXXXXXX"
""", file=sys.stderr)
        return 4
    log_event("slack_listen_start", {"channel": overlay["slack"]["channel_id"]})
    try:
        slack_runner.listen(overlay, poll_interval=args.poll_interval)
    except KeyboardInterrupt:
        log_event("slack_listen_stop", {"reason": "Ctrl-C"})
    return 0


def cmd_email_listen(args, overlay: dict) -> int:
    """Block forever, polling Gmail for trigger emails. Each one drives
    a cost-compiler plan and replies on the same thread."""
    try:
        from lib import email_runner
    except ImportError as e:
        print(str(e), file=sys.stderr)
        return 4
    if not (overlay.get("email") or {}).get("operator_email"):
        print("Overlay is missing email.operator_email. Add to identity.yaml:", file=sys.stderr)
        print("""
  email:
    trigger_label: "sai-trigger"
    from_address: "lutz+sai@example.com"
    operator_email: "lutz@example.com"
""", file=sys.stderr)
        return 4
    log_event("email_listen_start", {"label": (overlay.get("email") or {}).get("trigger_label")})
    try:
        email_runner.listen(overlay, poll_interval=args.poll_interval)
    except KeyboardInterrupt:
        log_event("email_listen_stop", {"reason": "Ctrl-C"})
    return 0


def cmd_extract_pre_bookings(args, overlay: dict) -> int:
    """Surface calendar events that look like pre-booked travel for this trip.

    Pre-bookings are flights and hotels added to the operator's calendar
    WEEKS before the trip start (often 1-6 months ahead). Each pre-booking
    is a candidate for an additional billable line on the customer
    invoice — the operator confirms before any QB write.

    The trip window is supplied via --start / --end (typically copied from
    the dates.md the operator approved in step 2).
    """
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    pre_window = args.pre_window_days
    min_days = args.min_days_before
    earliest = date.fromordinal(start.toordinal() - pre_window)
    latest = date.fromordinal(start.toordinal() - min_days)
    print(f"Trip: {start}..{end}. Pre-window scan: {earliest}..{latest}")

    try:
        events = calendar_fetch.list_events(
            earliest, latest, calendar_id=args.calendar_id or "primary",
        )
    except FileNotFoundError as e:
        print(f"Calendar auth missing: {e}", file=sys.stderr)
        return 4
    except Exception as e:
        print(f"Calendar fetch failed: {e}", file=sys.stderr)
        return 2

    print(f"Fetched {len(events)} event(s) in pre-window.")
    airline_hints = (overlay.get("sense_check") or {}).get("airline_hints") or []
    hotel_hints = (overlay.get("sense_check") or {}).get("hotel_hints") or []
    dest_hints: list[str] = list(args.destination_hints or [])
    # If the operator passed --customer, add it as a destination hint
    # (many trip events are named after the customer/program).
    if args.customer:
        dest_hints.append(args.customer)

    pre = trip_calendar.extract_pre_bookings(
        events, start, end,
        pre_window_days=pre_window,
        min_days_before=min_days,
        extra_airline_hints=airline_hints,
        extra_hotel_hints=hotel_hints,
        destination_hints=dest_hints,
    )

    if not pre:
        print("No pre-bookings detected.")
    else:
        print(f"\n{len(pre)} pre-booking candidate(s):")
        for p in pre:
            print(f"  [{p.kind:7s} {p.confidence:6s}] "
                  f"{p.event_start} ({p.days_before_trip}d before) — "
                  f"{p.event_summary}")
            if p.location:
                print(f"            loc: {p.location}")
            if p.description:
                print(f"            desc: {p.description[:120]}...")

    # Optional JSON dump for piping into a plan-builder.
    if args.json:
        out_path = Path(os.path.expanduser(args.json))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{
            "event_id": p.event_id,
            "event_summary": p.event_summary,
            "event_start": p.event_start.isoformat(),
            "days_before_trip": p.days_before_trip,
            "kind": p.kind,
            "confidence": p.confidence,
            "location": p.location,
            "description": p.description,
        } for p in pre]
        out_path.write_text(json.dumps(rows, indent=2))
        print(f"\nJSON written: {out_path}")

    log_event("extract_pre_bookings", {
        "trip_start": start.isoformat(),
        "trip_end": end.isoformat(),
        "candidates": len(pre),
        "high_confidence": sum(1 for p in pre if p.confidence == "high"),
        "kinds": {k: sum(1 for p in pre if p.kind == k) for k in ("flight", "hotel", "unknown")},
    })
    return 0


def cmd_await_approval(args, overlay: dict) -> int:
    """Open an approval gate. Block until the operator approves/rejects.

    Surfaces (--surface):
      cli   — interactive prompt at stdin (default; for Claude Code runs)
      file  — touch a sentinel file:
                <state_dir>/<request_id>.approve | .reject | .drop
              (used by Slack/email Phase C workers; they write the
              sentinel after parsing the operator's reply)

    Exit codes mirror approval state:
      0  APPROVED
      1  REJECTED
      2  DROPPED
      3  EXPIRED
      4  any internal/IO error
    """
    prompt = args.prompt
    if args.prompt_file:
        try:
            prompt = Path(os.path.expanduser(args.prompt_file)).read_text()
        except OSError as e:
            print(f"Couldn't read --prompt-file {args.prompt_file}: {e}", file=sys.stderr)
            return 4
    if not prompt:
        prompt = f"Approve the cost-compiler run for trip {args.trip!r}?"

    try:
        req = approval_lib.open_request(
            trip_slug=args.trip,
            surface=args.surface,
            prompt_text=prompt,
        )
    except OSError as e:
        print(f"Couldn't open approval state: {e}", file=sys.stderr)
        return 4

    print(f"Approval request opened: {req.request_id}")
    print(f"  surface: {args.surface}")
    print(f"  state:   {req.state_path}")
    log_event("approval_open", {
        "trip": args.trip, "request_id": req.request_id,
        "surface": args.surface,
    })

    timeout = args.timeout_seconds or None
    if args.surface == "cli":
        state = approval_lib.await_approval_cli(req, timeout_seconds=timeout)
    elif args.surface == "file":
        sentinel = Path(os.path.expanduser(
            args.sentinel_dir or
            "~/Library/Application Support/SAI/receipt-collector/sentinels"
        ))
        print(f"  sentinels at: {sentinel}")
        print(f"  to approve:  touch {sentinel}/{req.request_id}.approve")
        print(f"  to reject:   touch {sentinel}/{req.request_id}.reject")
        print(f"  to drop:     touch {sentinel}/{req.request_id}.drop")
        state = approval_lib.await_approval_file(
            req, sentinel_dir=sentinel,
            timeout_seconds=timeout,
        )
    else:
        print(f"Surface {args.surface!r} not yet wired in Phase B. "
              f"Slack/email arrive in Phase C.", file=sys.stderr)
        return 4

    log_event("approval_close", {
        "trip": args.trip, "request_id": req.request_id,
        "surface": args.surface, "state": state.value,
    })
    print(f"Final state: {state.value}")
    code_by_state = {
        approval_lib.ApprovalState.APPROVED: 0,
        approval_lib.ApprovalState.REJECTED: 1,
        approval_lib.ApprovalState.DROPPED: 2,
        approval_lib.ApprovalState.EXPIRED: 3,
        approval_lib.ApprovalState.OPEN: 4,
    }
    return code_by_state.get(state, 4)


def cmd_reconcile_billables(args, overlay: dict) -> int:
    """Match expected billables to QB Purchases over a date window.

    Plan.json may include `expected_billables`: a list of
    {name, txn_date, amount, currency, vendor?, purchase_id_hint?}.

    The subcommand:
      1. Calls QB to list every Purchase in [start, end] (across all
         payment accounts the overlay knows about).
      2. Runs lib.reconcile.reconcile to match expected vs found.
      3. Prints matched / missing / extras.
      4. Writes one JSONL audit row capturing the result.
      5. Exits with code 0 if all expected_billables matched,
         1 if any are missing (per SAI #6 fail-closed signal so a
         caller can hard-stop), 2 on errors.
    """
    plan_path = Path(os.path.expanduser(args.plan))
    if not plan_path.exists():
        print(f"No plan file at {plan_path}", file=sys.stderr)
        return 2
    plan = json.loads(plan_path.read_text())
    expected = reconcile_lib.expected_from_plan(plan)
    if not expected:
        print("Plan has no expected_billables array; nothing to reconcile.")
        return 0

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    client = qb_client_from_overlay(overlay)

    # Pull every Purchase in window, OPTIONALLY restricted to the
    # overlay's payment accounts so personal-card transactions aren't
    # surfaced as "extras."
    payment_account_ids: list[str] = []
    for acct in (overlay.get("payment_accounts") or {}).values():
        if isinstance(acct, dict) and acct.get("id"):
            payment_account_ids.append(str(acct["id"]))

    all_purchases = client.list_purchases(start, end)
    if payment_account_ids and not args.include_all_cards:
        all_purchases = [
            p for p in all_purchases
            if (p.get("AccountRef") or {}).get("value") in payment_account_ids
        ]
    print(f"Window {start}..{end}: {len(expected)} expected billable(s), "
          f"{len(all_purchases)} QB Purchase(s) "
          f"(payment-account filter: {bool(payment_account_ids) and not args.include_all_cards})")

    result = reconcile_lib.reconcile(
        expected, all_purchases,
        amount_tolerance_abs=args.amount_tol_abs,
        amount_tolerance_pct=args.amount_tol_pct,
        date_tolerance_days=args.date_tol_days,
    )

    print(f"\n{result.summary()}")
    print()
    if result.matched:
        print("MATCHED:")
        for m in result.matched:
            print(f"  ✓ {m.expected.name!r:40s} -> Purchase Id={m.purchase['Id']}  "
                  f"({m.purchase['TxnDate']}, {m.purchase['TotalAmt']})  "
                  f"[score={m.score:.2f}] {m.reason}")
    if result.missing:
        print()
        print("MISSING (no QB tx matched — paid cash or unknown card):")
        for e in result.missing:
            print(f"  ✗ {e.name!r:40s}  {e.txn_date}  {e.amount} {e.currency}  vendor={e.vendor!r}")
    if result.extras:
        print()
        print("EXTRAS (QB tx in window but not in expected_billables — review):")
        for p in result.extras:
            vendor = (p.get("EntityRef") or {}).get("name", "?")
            print(f"  ? Purchase Id={p['Id']}  {p.get('TxnDate')}  {p.get('TotalAmt')}  vendor={vendor!r}")

    log_event("reconcile_billables", {
        "trip": getattr(args, "trip", None),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "expected_count": len(expected),
        "purchase_count": len(all_purchases),
        "matched_ids": [m.purchase["Id"] for m in result.matched],
        "missing_names": [e.name for e in result.missing],
        "extra_ids": [p.get("Id") for p in result.extras],
    })

    # Exit codes: 0=all matched, 1=missing present, 2=error
    return 1 if result.missing else 0


def cmd_cache_secrets(args, overlay: dict) -> int:
    """One-shot: invoke `op` once to fetch the Anthropic API key (and any
    other long-lived secrets the skill needs), then stash them in macOS
    Keychain so the daemon can read them via `security` without ever
    triggering the macOS "op would like to access data from other apps"
    TCC prompt.

    Run this ONCE while at the keyboard. The daemon then never invokes
    `op` again — it reads from Keychain instead. Per SAI #7a.

    Idempotent — safe to re-run.
    """
    from lib import op_env
    secret_ref = (overlay.get("secrets") or {}).get("anthropic") or {}
    if not secret_ref:
        print("overlay['secrets']['anthropic'] missing.", file=sys.stderr)
        return 2
    op_item = secret_ref.get("op_item")
    op_vault = secret_ref.get("op_vault")
    field = secret_ref.get("field", "password")
    if not op_item or not op_vault:
        print(f"secret_ref incomplete: {secret_ref}", file=sys.stderr)
        return 2

    print(f"Fetching Anthropic API key via `op` (vault={op_vault!r}, item={op_item!r}).")
    print("If macOS prompts for permission, click 'Always Allow' on")
    print("BOTH dialogs (op + security). After this, the daemon never")
    print("invokes `op` again — all reads go through Keychain.")
    print()
    try:
        value = op_env.resolve_via_op_then_cache(
            logical_name="anthropic",
            op_item=op_item,
            op_vault=op_vault,
            op_field=field,
        )
    except Exception as e:
        print(f"Failed (Anthropic): {e}", file=sys.stderr)
        return 2
    print(f"  ✓ Anthropic key cached (sai-secret-anthropic, len={len(value)}).")

    # ALSO cache QuickBooks OAuth credentials so the daemon never
    # invokes `op` for QB API calls. Without this, every QBClient
    # construction triggers an `op item get` which can fire the
    # macOS "op would like to access data from other apps" prompt.
    qb_ref = (overlay.get("secrets") or {}).get("qb") or {}
    qb_item = qb_ref.get("op_item")
    qb_vault = qb_ref.get("op_vault")
    qb_field_map = qb_ref.get("fields") or {}
    if qb_item and qb_vault and qb_field_map:
        print(f"\nFetching QB OAuth creds via `op` (vault={qb_vault!r}, item={qb_item!r}).")
        try:
            from lib import op_secrets
            raw = op_secrets.get_all_fields(
                qb_item, list(qb_field_map.values()), vault=qb_vault,
            )
            for logical, op_field_name in qb_field_map.items():
                val = raw.get(op_field_name, "")
                if val:
                    op_env.cache_secret(f"qb-{logical}", val)
                    print(f"  ✓ qb-{logical} cached (len={len(val)})")
                else:
                    print(f"  ⚠ qb-{logical} empty — skipped", file=sys.stderr)
        except Exception as e:
            print(f"Failed (QB): {e}", file=sys.stderr)
            return 2
    print()
    print("Sanity check: reading all cached entries via `security` (no `op`)...")
    ok = 0
    for name in ["anthropic"] + [f"qb-{f}" for f in qb_field_map.keys()]:
        v = op_env.get_cached_secret(name)
        if v:
            ok += 1
        else:
            print(f"  ✗ {name} not retrievable", file=sys.stderr)
    print(f"  ✓ {ok} entries readable via Keychain without prompts.")
    log_event("cache_secrets", {
        "secrets": ["anthropic"] + [f"qb-{f}" for f in qb_field_map.keys()],
        "ok": ok,
    })
    return 0


def cmd_parse_trigger(args, overlay: dict) -> int:
    """Parse a free-form initiation string into a structured TripRequest.

    Deterministic rules-tier only — no LLM. Kept as a fallback path
    when the LLM agent (`propose-plan`) is unreachable. For normal
    operator-driven triggers, use `propose-plan` instead.
    """
    req = parse_trigger.parse(args.text, overlay)
    out = req.to_dict()
    print(json.dumps(out, indent=2))
    log_event("parse_trigger", out)
    return 0


def cmd_propose_plan(args, overlay: dict) -> int:
    """Run the cost-compiler trigger AGENT (Claude Haiku) on a free-form
    operator message. The agent has read-only access to QB customers,
    Google Calendar, and overlay metadata; it ends with ONE call to
    `propose_plan` that stages a JSON plan for the operator's approval
    gate.

    This replaces regex-based trigger parsing per operator decision
    2026-05-20: "don't do a parser; use Haiku to evaluate what I want
    and propose a plan."

    Exit codes:
      0  plan staged successfully
      1  agent returned a clarification (no plan staged)
      2  agent ran but errored mid-loop
      3  daily LLM cap hit (fell back to rules tier)
    """
    print(f"Agent: thinking about {args.text!r}...")
    result = cost_compiler_agent.run_agent(
        source_text=args.text,
        overlay=overlay,
    )
    inv = result.invocation
    print("─" * 72)
    print(result.operator_message)
    print("─" * 72)
    if inv:
        print(f"\nInvocation: {inv.invocation_id}")
        print(f"  model:    {inv.model_used}")
        print(f"  iters:    {inv.iterations}")
        print(f"  tools:    {[t['tool'] for t in inv.tool_calls]}")
        print(f"  cost:     ${inv.cost_usd:.4f}")
        print(f"  reason:   {inv.terminated_reason}")
        if inv.error:
            print(f"  error:    {inv.error}")
    if result.staged_plan_path:
        print(f"\nStaged plan: {result.staged_plan_path}")
        log_event("propose_plan", {
            "invocation_id": inv.invocation_id if inv else None,
            "staged_path": result.staged_plan_path,
            "proposal_id": (result.proposed_plan or {}).get("proposal_id"),
            "trip_slug": (result.proposed_plan or {}).get("trip_slug"),
            "cost_usd": inv.cost_usd if inv else 0.0,
        })
        return 0
    if inv and inv.terminated_reason == "budget_exceeded":
        return 3
    if inv and inv.terminated_reason in ("error", "iteration_cap"):
        return 2
    log_event("propose_plan_clarification", {
        "invocation_id": inv.invocation_id if inv else None,
        "operator_text": args.text[:300],
    })
    return 1


def cmd_scan_cards(args, overlay: dict) -> int:
    client = qb_client_from_overlay(overlay)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    payment_accounts = overlay.get("payment_accounts", {})
    rows = client.list_purchases(start, end)
    print(f"Purchases {start}..{end}: {len(rows)} total across all payment accounts.\n")
    # Group by payment account
    by_acc: dict[str, list[dict]] = {}
    for p in rows:
        acc_id = (p.get("AccountRef") or {}).get("value", "?")
        by_acc.setdefault(acc_id, []).append(p)
    for acc_id, plist in sorted(by_acc.items()):
        acc_label = next(
            (label for label, info in payment_accounts.items()
             if str(info.get("id")) == str(acc_id)),
            f"Account {acc_id}",
        )
        print(f"=== {acc_label} (id={acc_id}) — {len(plist)} txns ===")
        for p in sorted(plist, key=lambda x: x.get("TxnDate", "")):
            vname = (p.get("EntityRef") or {}).get("name", "?")
            cur = p.get("CurrencyRef", {}).get("value")
            print(f"  {p['TxnDate']}  Id={p['Id']}  {p.get('TotalAmt')} {cur}  vendor={vname!r}")
        print()
    log_event("scan_cards", {"start": args.start, "end": args.end, "total": len(rows)})
    return 0


def cmd_search_receipts(args, overlay: dict) -> int:
    senders = overlay.get("gmail_senders", [])
    if not senders:
        print("Overlay has no gmail_senders configured.", file=sys.stderr)
        print("Add a 'gmail_senders:' list (e.g., ['noreply@example.com']) to your overlay.", file=sys.stderr)
        return 2
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    query = build_receipt_query(senders, start, end)
    print(query)
    log_event("search_receipts_query_built", {"start": args.start, "end": args.end, "senders": senders})
    return 0


def cmd_create_purchases(args, overlay: dict) -> int:
    plan = json.loads(Path(args.plan).read_text())
    client = qb_client_from_overlay(overlay)
    trip_slug = args.trip
    created, skipped, failed = [], [], []
    trip_start = date.fromisoformat(plan["trip_start"])
    trip_end = date.fromisoformat(plan["trip_end"])
    for r in plan["receipts"]:
        marker = f"[sai-receipts:{trip_slug}] {r['name']}"
        existing = client.find_purchase_by_marker(marker, trip_start, trip_end)
        if existing:
            print(f"  SKIP {r['name']!r} — Purchase Id={existing['Id']}")
            skipped.append({"name": r["name"], "id": existing["Id"]})
            continue
        try:
            body = purchases_lib.build_purchase(r, marker, customer_name=plan.get("customer_name"))
            result = client.create_purchase(body)
            new_id = result.get("Id")
            print(f"  CREATED {r['name']!r}  {r['amount']} {r['currency']}  →  Id={new_id}")
            created.append({"name": r["name"], "id": new_id})
        except Exception as e:
            print(f"  FAILED {r['name']!r}: {e}")
            failed.append({"name": r["name"], "error": str(e)})
    print(f"\nSummary: created={len(created)}  skipped={len(skipped)}  failed={len(failed)}")
    log_event("create_purchases", {"trip": trip_slug, "created": created, "skipped": skipped, "failed": failed})
    return 0 if not failed else 2


def cmd_create_invoice(args, overlay: dict) -> int:
    plan = json.loads(Path(args.plan).read_text())
    client = qb_client_from_overlay(overlay)
    trip_slug = args.trip
    customer = client.find_customer_by_name(plan["customer_name"])
    if not customer:
        print(f"Customer {plan['customer_name']!r} not found.", file=sys.stderr)
        return 1
    marker = f"[sai-invoice:{trip_slug}]"
    trip_start = date.fromisoformat(plan["trip_start"])
    trip_end = date.fromisoformat(plan["trip_end"])
    existing = client.find_invoice_by_marker(marker, trip_start, trip_end)
    if existing:
        print(f"SKIP — Invoice already exists. Id={existing['Id']}  Total={existing.get('TotalAmt')}")
        return 0

    # Currency resolution order (Phase A.3): CLI override → plan → customer → USD
    cli_currency = getattr(args, "currency", None)
    invoice_currency = (
        (cli_currency or "").upper()
        or (plan.get("invoice_currency") or "").upper()
        or ((customer.get("CurrencyRef") or {}).get("value") or "").upper()
        or "USD"
    )

    # FX audit-log callback: every FX lookup writes one event row to the
    # JSONL audit log so we can reconstruct exactly which rate was used
    # on which line.
    fx_events: list[dict] = []
    def _fx_log(row: dict) -> None:
        fx_events.append(row)
        log_event("fx_lookup", {
            "trip": trip_slug,
            "invoice_marker": marker,
            **row,
        })

    fx_fallback = (overlay.get("fx") or {}).get("default_table") or {}

    body = invoices_lib.build_invoice(
        customer=customer,
        lines=plan["invoice_lines"],
        currency=invoice_currency,
        marker=marker,
        po_number=plan.get("po_number"),
        header_memo=plan.get("memo"),
        on_fx_log=_fx_log,
        fx_fallback_table=fx_fallback,
    )
    body = {k: v for k, v in body.items() if v is not None}
    result = client.create_invoice(body)
    inv_id = result.get("Id")
    print(f"CREATED Invoice Id={inv_id}  Total={result.get('TotalAmt')} {result.get('CurrencyRef', {}).get('value')}")
    if fx_events:
        print(f"FX lookups applied: {len(fx_events)} line(s)")
        for ev in fx_events:
            src_amt = ev.get('original_unit_rate')
            conv_amt = ev.get('converted_unit_rate')
            from_ccy = ev.get('from_ccy')
            to_ccy = ev.get('to_ccy')
            src_str = "?" if src_amt is None else f"{src_amt:.2f}"
            conv_str = "?" if conv_amt is None else f"{conv_amt:.2f}"
            print(f"  {from_ccy}→{to_ccy} @ {ev['rate']:.4f} ({ev['source']}) "
                  f"on {ev['on_date']}  unit {src_str} → {conv_str}")
    log_event("create_invoice", {
        "trip": trip_slug,
        "invoice_id": inv_id,
        "total": result.get("TotalAmt"),
        "currency": invoice_currency,
        "fx_lookups": len(fx_events),
    })
    return 0


def cmd_tag_purchases(args, overlay: dict) -> int:
    """Append "Billed as expenses to <customer>" to PrivateNote on every
    Purchase that carries the trip marker. Also prints a list of Purchase
    IDs so the operator can apply the QB Tag in the UI (the v3 API has no
    Tag write endpoint)."""
    client = qb_client_from_overlay(overlay)
    trip_slug = args.trip
    customer = args.customer
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    exclude_ids = set(filter(None, (args.exclude or "").split(",")))

    found = qb_tags.find_purchases_for_trip(client, trip_slug, start, end)
    found = [p for p in found if p["Id"] not in exclude_ids]
    print(f"Found {len(found)} Purchase(s) marked for trip {trip_slug!r}"
          f" between {start} and {end}" + (f" (after excluding {len(exclude_ids)})" if exclude_ids else ""))

    updated, skipped, failed = [], [], []
    for p in found:
        try:
            if qb_tags.already_billed(p, customer):
                print(f"  SKIP Id={p['Id']} — already memo-flagged for {customer!r}")
                skipped.append(p["Id"])
                continue
            qb_tags.mark_billed(client, p, customer)
            print(f"  UPDATED Id={p['Id']} — appended 'Billed as expenses to {customer}'")
            updated.append(p["Id"])
        except Exception as e:
            print(f"  FAILED Id={p['Id']}: {e}")
            failed.append((p["Id"], str(e)))

    if found:
        print()
        print(qb_tags.manual_tag_report(found, customer))
    print(f"\nSummary: updated={len(updated)}  skipped={len(skipped)}  failed={len(failed)}")
    log_event("tag_purchases", {
        "trip": trip_slug, "customer": customer,
        "updated": updated, "skipped": skipped, "failed": failed,
    })
    return 0 if not failed else 2


def cmd_download_receipts(args, overlay: dict) -> int:
    """Download Gmail receipt threads (body + attachments) to a per-trip
    folder under ~/Downloads/sai-receipts-<trip>/<thread_id>/."""
    from lib import gmail_fetch
    senders = overlay.get("gmail_senders") or []
    if not senders:
        print("Overlay has no gmail_senders configured.", file=sys.stderr)
        return 2
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    trip = args.trip
    out_root = Path(os.path.expanduser(args.out_root)) / f"sai-receipts-{trip}"
    out_root.mkdir(parents=True, exist_ok=True)
    subject_kw = overlay.get("gmail_receipt_subject_keywords") or [
        "receipt", "eTicket", "charge summary", "Thanks for riding",
    ]
    # Wrap subject keywords with subject: prefix so we filter on subject only,
    # not body — this drops promo emails that mention "receipt" in their pitch.
    subject_group = [f'subject:"{kw}"' if " " in kw else f"subject:{kw}" for kw in subject_kw]
    query = gmail_fetch.build_query(senders, start, end, keywords=subject_group)
    print(f"Gmail query: {query}")
    print(f"Output:      {out_root}")
    try:
        svc = gmail_fetch._build_service()
    except ImportError as e:
        print(f"\nPython Gmail client not installed:\n{e}", file=sys.stderr)
        print("\nFallback: use the Gmail MCP in Claude Code with the query above,", file=sys.stderr)
        print("and save attachments to the Output path.", file=sys.stderr)
        return 3
    thread_ids = gmail_fetch.search_threads(svc, query)
    print(f"Threads found: {len(thread_ids)}")
    manifest = []
    for tid in thread_ids:
        result = gmail_fetch.download_thread(svc, tid, out_root / tid)
        n_files = len(result["files"])
        body = []
        if result.get("has_text"): body.append("text")
        if result.get("has_html"): body.append("html")
        body_str = ("+" + "+".join(body)) if body else "EMPTY"
        print(f"  {tid}  {result['subject'][:60]!r}  files={n_files} {body_str}")
        manifest.append(result)
    (out_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log_event("download_receipts", {"trip": trip, "thread_count": len(thread_ids), "out_root": str(out_root)})
    print(f"\nDone. {len(thread_ids)} threads saved to {out_root}")
    return 0


def cmd_match_receipts(args, overlay: dict) -> int:
    """For each trip-marked QB Purchase: derive a targeted Gmail query,
    fetch matching threads in-memory, render each to PDF, and (unless
    --no-upload) attach the PDFs to the corresponding QB Purchase.

    Output: only PDFs under <out-root>/sai-receipts-<trip>/. No HTML,
    no body.txt, no index.md — the PDF is the artifact."""
    from lib import gmail_fetch, pdf_render, qb_attachments

    client = qb_client_from_overlay(overlay)
    trip = args.trip
    customer = args.customer
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    out_root = Path(os.path.expanduser(args.out_root)) / f"sai-receipts-{trip}"
    out_root.mkdir(parents=True, exist_ok=True)

    purchases = qb_tags.find_purchases_for_trip(client, trip, start, end)
    print(f"Trip {trip!r}: {len(purchases)} Purchase(s) marked for matching")
    if not purchases:
        return 0

    try:
        svc = gmail_fetch._build_service()
    except ImportError as e:
        print(f"\nPython Gmail client not installed:\n{e}", file=sys.stderr)
        return 3

    n_pdfs = 0
    n_uploaded = 0
    n_skipped_onsite = 0
    n_no_match = 0

    for p in sorted(purchases, key=lambda x: x.get("TxnDate", "")):
        pid = p["Id"]
        vname = (p.get("EntityRef") or {}).get("name", "?")
        amt = p.get("TotalAmt")
        cur = (p.get("CurrencyRef") or {}).get("value", "?")
        amt_str = f"{amt:.2f}" if isinstance(amt, (int, float)) else str(amt)
        q = receipt_match.derive_search(p, overlay=overlay)

        print(f"\n--- Purchase Id={pid}  {p.get('TxnDate')}  {amt_str} {cur}  vendor={vname!r}")

        if q is None:
            print(f"    skip — no email receipt expected (paid on site)")
            n_skipped_onsite += 1
            continue

        print(f"    query: {q}")
        thread_ids = gmail_fetch.search_threads(svc, q, max_threads=20)
        if not thread_ids:
            print(f"    no matching Gmail thread")
            n_no_match += 1
            continue

        for tid in thread_ids:
            info = gmail_fetch.fetch_thread(svc, tid)
            vendor_slug = re.sub(r"[^a-zA-Z0-9]+", "-", vname.lower()).strip("-")[:30] or "vendor"
            subject_slug = re.sub(r"[^a-zA-Z0-9]+", "-", (info["subject"] or "").lower()).strip("-")[:40]
            pdf_name = (
                f"purchase-{pid}-{p.get('TxnDate')}-{vendor_slug}"
                + (f"-{subject_slug}" if subject_slug else "")
                + f"-{tid[:8]}.pdf"
            )
            pdf_path = out_root / pdf_name
            try:
                kind = pdf_render.render_pdf(
                    pdf_path,
                    body_text=info["body_text"],
                    html_body=info.get("body_html", ""),
                    vendor=vname, purchase_id=str(pid),
                    amount=amt_str, currency=cur,
                    subject=info["subject"], date_iso=info["date_iso"] or p.get("TxnDate", ""),
                    customer=customer,
                )
                size = pdf_path.stat().st_size
                print(f"      PDF ({kind}): {pdf_name}  ({size:,} bytes)")
                n_pdfs += 1
            except Exception as e:
                print(f"      PDF FAILED for thread {tid}: {e}")
                continue

            if not args.no_upload:
                try:
                    note_prefix = f"Receipt for trip {trip} billable to {customer}." if customer else f"Receipt for trip {trip}."
                    att = qb_attachments.upload_for_purchase(
                        client, pid, pdf_path, trip_slug=trip, note_prefix=note_prefix,
                    )
                    print(f"      QB attached → Attachable Id={att.get('Id')}")
                    n_uploaded += 1
                except Exception as e:
                    print(f"      QB attach FAILED: {e}")

    print(f"\nSummary: {n_pdfs} PDFs rendered, {n_uploaded} attached to QB, "
          f"{n_skipped_onsite} on-site (no email), {n_no_match} no-match")
    log_event("match_receipts_to_purchases", {
        "trip": trip, "customer": customer,
        "purchases": len(purchases),
        "pdfs": n_pdfs, "uploaded": n_uploaded,
        "skipped_onsite": n_skipped_onsite, "no_match": n_no_match,
    })
    return 0


def cmd_attach_onsite_photos(args, overlay: dict) -> int:
    """Pull on-site receipt photos the operator forwarded to QB's Receipts
    inbox, match each to a trip-marked Purchase by subject overlap, wrap
    in a one-page PDF with the SAI banner, and upload as a QB Attachable."""
    from lib import gmail_fetch, pdf_render, qb_attachments

    client = qb_client_from_overlay(overlay)
    trip = args.trip
    customer = args.customer
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    out_root = Path(os.path.expanduser(args.out_root)) / f"sai-photos-{trip}"
    out_root.mkdir(parents=True, exist_ok=True)

    inboxes = overlay.get("qb_receipts_inboxes") or []
    if not inboxes:
        print("Overlay has no qb_receipts_inboxes configured.", file=sys.stderr)
        return 2

    all_purchases = qb_tags.find_purchases_for_trip(client, trip, start, end)
    # Only on-site purchases (no email receipt expected) need photo matching.
    # Items that DO have email receipts are already handled by match-receipts-to-purchases.
    purchases = [p for p in all_purchases if receipt_match.derive_search(p, overlay=overlay) is None]
    print(f"On-site Purchases for {trip!r}: {len(purchases)} (of {len(all_purchases)} total)")
    if not purchases:
        print(f"No on-site Purchases to match. Done.")
        return 0

    try:
        svc = gmail_fetch._build_service()
    except ImportError as e:
        print(f"Python Gmail client not installed: {e}", file=sys.stderr)
        return 3

    q = forwarded_receipts.build_query(inboxes, start, end + _td_days(7))
    print(f"Gmail query: {q}\n")
    thread_ids = gmail_fetch.search_threads(svc, q, max_threads=80)
    print(f"{len(thread_ids)} forwarded-receipt thread(s) found")

    # Pull each thread's metadata (subject + attachments).
    threads_meta = []
    for tid in thread_ids:
        info = gmail_fetch.fetch_thread(svc, tid)
        img_atts = [(name, mime, raw) for (name, mime, raw) in info["attachments"]
                    if mime.startswith("image/")]
        if not img_atts:
            continue
        threads_meta.append({**info, "image_attachments": img_atts})

    print(f"  {len(threads_meta)} with image attachments")

    # Match threads → Purchases. customer name (e.g., "<your-customer>") in subject
    # is a hard filter — kills the "Travel food" / "OMMAX hotel" false
    # positives that share generic tokens with the trip's Purchases.
    matched = forwarded_receipts.match_threads_to_purchases(
        threads_meta, purchases, customer=customer,
    )
    n_pdfs = 0
    n_uploaded = 0
    for p in purchases:
        pid = p["Id"]
        threads = matched.get(pid, [])
        if not threads:
            continue
        vname = (p.get("EntityRef") or {}).get("name", "?")
        amt = p.get("TotalAmt")
        cur = (p.get("CurrencyRef") or {}).get("value", "?")
        amt_str = f"{amt:.2f}" if isinstance(amt, (int, float)) else str(amt)
        print(f"\n--- Purchase Id={pid}  {vname}  {amt_str} {cur}")
        for t in threads:
            tid = t["thread_id"]
            print(f"    match thread={tid}  score={t['_score']}  subject={t['subject']!r}")
            # Save raw photos, then a downsized variant for PDF wrap (the
            # full-res 4032x3024 phone JPEGs would balloon the PDF to 20+ MB).
            from PIL import Image
            saved_paths = []
            for i, (name, mime, raw) in enumerate(t["image_attachments"]):
                ext = "jpg" if mime == "image/jpeg" else mime.split("/")[-1]
                raw_path = out_root / f"purchase-{pid}-{tid[:8]}-{i+1}.{ext}"
                raw_path.write_bytes(raw)
                small_path = out_root / f"purchase-{pid}-{tid[:8]}-{i+1}.small.jpg"
                im = Image.open(raw_path)
                im.thumbnail((1800, 1800))
                im.save(small_path, "JPEG", quality=85, optimize=True)
                saved_paths.append(small_path)
                print(f"      saved {raw_path.name}  ({len(raw):,} bytes), "
                      f"resized {small_path.name}  ({small_path.stat().st_size:,} bytes)")
            # Render the photos as a single PDF (one image per page) with banner
            pdf_name = f"purchase-{pid}-onsite-{tid[:8]}.pdf"
            pdf_path = out_root / pdf_name
            pdf_render.image_to_pdf(
                pdf_path, saved_paths,
                vendor=vname, purchase_id=str(pid),
                amount=amt_str, currency=cur,
                subject=t["subject"], date_iso=t.get("date_iso", "") or p.get("TxnDate", ""),
                customer=customer,
            )
            print(f"      PDF: {pdf_name}  ({pdf_path.stat().st_size:,} bytes)")
            n_pdfs += 1
            if not args.no_upload:
                try:
                    note_prefix = (f"On-site receipt photo for trip {trip}"
                                   + (f", billable to {customer}." if customer else "."))
                    att = qb_attachments.upload_for_purchase(
                        client, pid, pdf_path, trip_slug=trip, note_prefix=note_prefix,
                    )
                    print(f"      QB attached → Attachable Id={att.get('Id')}")
                    n_uploaded += 1
                except Exception as e:
                    print(f"      QB attach FAILED: {e}")

    print(f"\nSummary: {n_pdfs} photo-PDFs rendered, {n_uploaded} attached to QB")
    log_event("attach_onsite_photos", {
        "trip": trip, "customer": customer,
        "purchases": len(purchases),
        "pdfs": n_pdfs, "uploaded": n_uploaded,
    })
    return 0


def _td_days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def cmd_gphotos_auth(args, overlay: dict) -> int:
    """One-time OAuth grant for a Google Photos account. Saves token at
    ~/.SAI/gphotos_token_<label>.json so multiple accounts can coexist
    (e.g., work mailbox vs personal mailbox)."""
    try:
        creds = google_photos.auth_for_account(args.account)
    except FileNotFoundError as e:
        print(f"Missing Google OAuth client: {e}", file=sys.stderr); return 2
    print(f"OAuth token saved for account label {args.account!r}")
    print(f"  valid={creds.valid}  expiry={creds.expiry}")
    log_event("gphotos_auth", {"account": args.account, "valid": bool(creds.valid)})
    return 0


def cmd_scan_gphotos(args, overlay: dict) -> int:
    """Search a Google Photos library for media items in a date window.
    Downloads candidate receipt photos to <out-root>/sai-gphotos-<trip>/.
    Returns empty result if Google's March-2025 partner-only restriction
    blocks the search."""
    creds = google_photos.auth_for_account(args.account)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    try:
        items = google_photos.search_media(creds, start, end, max_items=200)
    except RuntimeError as e:
        print(f"\nGoogle Photos API call failed: {e}", file=sys.stderr)
        print("Most likely cause: Google restricts the Photos Library API to "
              "partner-program apps (March 2025 change). Fall back to forwarding "
              "the photo to a Gmail address scanned by attach-onsite-photos.", file=sys.stderr)
        return 3
    print(f"{len(items)} media item(s) found between {start} and {end}")
    if not items:
        return 0
    out_root = Path(os.path.expanduser(args.out_root)) / f"sai-gphotos-{args.trip}"
    out_root.mkdir(parents=True, exist_ok=True)
    for item in items:
        try:
            path = google_photos.download_media(creds, item, out_root)
            print(f"  saved {path.name}")
        except Exception as e:
            print(f"  FAILED {item.get('filename')}: {e}")
    log_event("scan_gphotos", {"trip": args.trip, "account": args.account,
                                "items": len(items), "out_root": str(out_root)})
    return 0


def cmd_extract_amounts(args, overlay: dict) -> int:
    """Run vision OCR against each image in a directory and print the
    extracted total/currency/date. Writes one llm_costs.jsonl entry per
    image. Advisory only — does NOT write back to QB.

    Cascade (Phase D.1): when `--local-first` is on (default), each
    image is first sent to a local Llava model via Ollama (free). Only
    if the local tier returns low confidence or can't parse a total do
    we escalate to Claude Haiku. This satisfies SAI #1 + #12 (try
    cheap tier first; cloud is the long tail).

    Pass `--no-local-first` to force cloud-only (e.g., when Llava isn't
    pulled, or you want a controlled regression run)."""
    folder = Path(os.path.expanduser(args.folder))
    if not folder.exists():
        print(f"No such folder: {folder}", file=sys.stderr); return 2
    images = sorted([p for p in folder.iterdir()
                     if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic", ".webp")
                     and ".small" not in p.name])
    print(f"{len(images)} image(s) to scan in {folder}")
    if args.local_first:
        print(f"Cascade: local ({args.local_model}) → cloud ({args.model}) on low-confidence")
    else:
        print(f"Cloud-only mode ({args.model})")
    total_usd = 0.0
    tier_counts = {"local": 0, "cloud": 0, "cloud-only": 0}
    secret_ref = ((overlay.get("secrets") or {}).get("anthropic")) or {}
    for img in images:
        try:
            res, tier = vision_extract.extract_receipt_cascaded(
                img,
                cloud_model=args.model,
                local_model=args.local_model,
                secret_ref=secret_ref,
                overlay=overlay,
                skill_name="receipt-collector",
                step_name="extract_amounts",
                local_first=args.local_first,
            )
        except llm_costs.BudgetExceeded as bx:
            print(f"\nBUDGET HARD STOP: {bx}", file=sys.stderr)
            print(f"  Stopped before {img.name}. Already-processed images this run "
                  f"are logged to ~/Library/Logs/SAI/llm_costs.jsonl.", file=sys.stderr)
            log_event("budget_exceeded", {
                "skill": bx.skill, "step": bx.step,
                "cap_usd": bx.cap_usd, "today_usd": bx.today_usd,
                "upcoming_usd": bx.upcoming_usd, "stopped_at": img.name,
            })
            return 3
        except Exception as e:
            print(f"  {img.name}  FAILED: {e}")
            continue
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        # Only log a cost row when we actually called a paid tier.
        if res.usd_cost > 0:
            llm_costs.log_call(
                skill="receipt-collector", step="extract_amounts",
                model=args.model,
                input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                usd_cost=res.usd_cost,
                note=f"file={img.name} tier={tier} total={res.total} {res.currency}",
            )
        else:
            # Track local-tier calls separately so we can graph the
            # cascade hit rate.
            llm_costs.log_call(
                skill="receipt-collector", step="extract_amounts",
                model=args.local_model,
                input_tokens=0, output_tokens=0, usd_cost=0.0,
                note=f"file={img.name} tier={tier} local",
            )
        total_usd += res.usd_cost
        amt = f"{res.total} {res.currency}" if res.total else "??"
        print(f"  {img.name}  [tier={tier}]")
        print(f"    total={amt}  date={res.date_iso}  vendor={res.vendor!r}  conf={res.confidence}")
        if res.usd_cost > 0:
            print(f"    cost={res.usd_cost:.4f} USD  tokens=in:{res.input_tokens}/out:{res.output_tokens}")
        else:
            print(f"    cost=$0 (local)")
        if res.notes:
            print(f"    notes: {res.notes}")
    print(f"\nTotal cloud LLM cost this run: {total_usd:.4f} USD")
    print(f"Tier hits: {tier_counts}")
    print(f"Today's total: {llm_costs.today_usd_total('receipt-collector'):.4f} USD")
    return 0


def cmd_sense_check(args, overlay: dict) -> int:
    """Run a date-plausibility check on every Purchase tagged for a trip.

    Two-tier:
      1. Deterministic gate (free, fast)
      2. Local-LLM gate (Ollama, llama3.2:1b default) only on MAYBE
    Verdicts: YES / MAYBE / NO with a reason.

    Exit code 0 = all clear, 2 = at least one NO, 1 = at least one MAYBE
    after the LLM pass — operator should review either way.
    """
    client = qb_client_from_overlay(overlay)
    trip = args.trip
    customer = args.customer
    trip_start = date.fromisoformat(args.start)
    trip_end = date.fromisoformat(args.end)

    purchases = qb_tags.find_purchases_for_trip(
        client, trip,
        # Widen the find-window so we can catch tags that fell outside the
        # trip dates — that's exactly the bug we're hunting.
        trip_start - _td_days(365), trip_end + _td_days(30),
    )
    print(f"sense-check trip={trip!r}  window={trip_start}..{trip_end}  customer={customer!r}")
    print(f"  {len(purchases)} Purchase(s) marked for this trip\n")

    sc_cfg = overlay.get("sense_check") or {}
    extra_airlines = sc_cfg.get("airline_hints") or []
    extra_hotels = sc_cfg.get("hotel_hints") or []

    results = []
    n_no = 0
    n_maybe = 0
    for p in sorted(purchases, key=lambda x: x.get("TxnDate", "")):
        pid = p["Id"]
        txn = date.fromisoformat(p["TxnDate"])
        vendor = (p.get("EntityRef") or {}).get("name", "?")
        desc = ((p.get("Line") or [{}])[0]).get("Description", "")
        amt = p.get("TotalAmt") or 0.0
        cur = (p.get("CurrencyRef") or {}).get("value", "?")
        check = sense_check.check_item(
            purchase_id=pid, txn_date=txn,
            vendor=vendor, description=desc,
            amount=amt, currency=cur,
            customer=customer, trip_start=trip_start, trip_end=trip_end,
            llm_model=args.model,
            extra_airline_hints=extra_airlines,
            extra_hotel_hints=extra_hotels,
        )
        results.append(check)
        glyph = {"YES": "OK ", "MAYBE": "? ", "NO": "NO "}[check.verdict.value]
        print(f"  [{glyph}] Id={pid:<6}  {txn}  {amt:>10.2f} {cur}  {vendor}")
        print(f"          desc: {desc[:90]!r}")
        print(f"          verdict={check.verdict.value}  via {check.source}")
        print(f"          reason: {check.reason}")
        if check.verdict is sense_check.Verdict.NO:
            n_no += 1
        elif check.verdict is sense_check.Verdict.MAYBE:
            n_maybe += 1

    print(f"\nSummary: {len(results)} checked, {n_no} NO, {n_maybe} MAYBE")
    log_event("sense_check", {
        "trip": trip, "customer": customer,
        "window": [str(trip_start), str(trip_end)],
        "checks": [
            {"id": r.purchase_id, "txn_date": str(r.txn_date),
             "vendor": r.vendor, "verdict": r.verdict.value,
             "source": r.source, "reason": r.reason}
            for r in results
        ],
        "n_no": n_no, "n_maybe": n_maybe,
    })
    if n_no:
        return 2
    if n_maybe:
        return 1
    return 0


def cmd_cleanup_pass(args, overlay: dict) -> int:
    """Walk recent Purchases and propose changes per the operator's
    bookkeeping rules.

    Per SAI #20 (Reflection may suggest, never auto-apply), this writes
    a markdown proposal doc; nothing is applied to QB without operator
    confirmation. The operator can then either apply changes manually
    in the QB UI or run a future `apply-rule --rule <id> --confirm`.
    """
    rules_path = Path(os.path.expanduser(
        args.rules or overlay.get("bookkeeping_rules_path") or
        "~/SAI/skills/receipt-collector/bookkeeping-rules.md"
    ))
    if not rules_path.exists():
        print(f"No rules file at {rules_path}", file=sys.stderr)
        return 1
    rules = cleanup_lib.parse_rules(rules_path.read_text())
    print(f"Cleanup pass — {len(rules)} rule(s) loaded from {rules_path}")
    for r in rules:
        print(f"  {r.rule_id} — {r.title}  ({len(r.trigger_keywords)} trigger keywords)")
    if not rules:
        print("No parseable rules found; aborting.", file=sys.stderr)
        return 0

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    client = qb_client_from_overlay(overlay)
    print(f"\nLoading Purchases {start}..{end}...")
    purchases = client.list_purchases(start, end)
    # Restrict to overlay payment_accounts unless --include-all-cards.
    if not args.include_all_cards:
        pay_ids: list[str] = []
        for acct in (overlay.get("payment_accounts") or {}).values():
            if isinstance(acct, dict) and acct.get("id"):
                pay_ids.append(str(acct["id"]))
        if pay_ids:
            purchases = [p for p in purchases if (p.get("AccountRef") or {}).get("value") in pay_ids]
    print(f"  {len(purchases)} Purchase(s) in scope")

    proposals = cleanup_lib.propose(
        purchases, rules,
        trip_start=date.fromisoformat(args.trip_start) if args.trip_start else None,
        trip_end=date.fromisoformat(args.trip_end) if args.trip_end else None,
    )
    print(f"\n{len(proposals)} proposal(s) generated.")
    by_conf: dict[str, int] = {}
    for pr in proposals:
        by_conf[pr.confidence] = by_conf.get(pr.confidence, 0) + 1
    for c in ("high", "medium", "low"):
        if c in by_conf:
            print(f"  {c}: {by_conf[c]}")

    out_path = Path(os.path.expanduser(args.out or "~/Downloads/cleanup-proposals.md"))
    cleanup_lib.write_proposal_doc(proposals, out_path)
    print(f"\nProposal doc: {out_path}")

    log_event("cleanup_pass", {
        "rules_file": str(rules_path),
        "purchase_count": len(purchases),
        "rules_count": len(rules),
        "proposals": len(proposals),
        "by_confidence": by_conf,
        "out": str(out_path),
        "applied": 0,
    })
    return 0


# ---------------- main ----------------

def main() -> int:
    p = argparse.ArgumentParser(prog="receipt-collector")
    p.add_argument("--overlay", default=os.environ.get("SAI_RECEIPT_OVERLAY", "~/SAI/skills/receipt-collector"),
                   help="Path to the operator overlay folder (env: SAI_RECEIPT_OVERLAY).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check-auth").set_defaults(fn=cmd_check_auth)

    s = sub.add_parser("slack-listen",
        help="Long-poll the configured Slack channel for triggers. Each message "
             "drives a cost-compiler plan with status replies on the same thread.")
    s.add_argument("--poll-interval", type=float, default=5.0)
    s.set_defaults(fn=cmd_slack_listen)

    s = sub.add_parser("email-listen",
        help="Poll Gmail for trigger emails matching the overlay's label. Each one "
             "drives a cost-compiler plan with status replies on the same thread.")
    s.add_argument("--poll-interval", type=float, default=60.0)
    s.set_defaults(fn=cmd_email_listen)

    s = sub.add_parser("extract-pre-bookings",
        help="Find calendar events that look like flights/hotels pre-booked weeks before the trip.")
    s.add_argument("--start", required=True, help="Trip start YYYY-MM-DD.")
    s.add_argument("--end", required=True, help="Trip end YYYY-MM-DD.")
    s.add_argument("--pre-window-days", type=int, default=180,
                   help="How far back to look for pre-bookings (default 180 days).")
    s.add_argument("--min-days-before", type=int, default=7,
                   help="Minimum gap between event and trip start (default 7 days; closer events are part of the trip).")
    s.add_argument("--calendar-id", default="primary")
    s.add_argument("--customer", default=None,
                   help="Customer name (e.g. INSEAD); used as a destination hint.")
    s.add_argument("--destination-hints", nargs="*", default=None,
                   help="Optional destination keywords/airport codes (e.g. CDG Paris). Overlay airline/hotel hints are loaded automatically.")
    s.add_argument("--json", default=None,
                   help="If set, write candidate list to this JSON file (for piping into a plan-builder).")
    s.set_defaults(fn=cmd_extract_pre_bookings)

    s = sub.add_parser("await-approval",
        help="Open an approval gate. Blocks until the operator approves/rejects/aborts.")
    s.add_argument("--trip", required=True)
    s.add_argument("--surface", choices=["cli", "file"], default="cli",
                   help="Where to ask. CLI = stdin. file = sentinel file. "
                        "Slack/email Phase C will write the sentinel after parsing a reply.")
    s.add_argument("--prompt", default=None, help="Inline prompt text shown to operator.")
    s.add_argument("--prompt-file", default=None,
                   help="Path to a markdown file (e.g. final-review.md) to show instead of --prompt.")
    s.add_argument("--timeout-seconds", type=int, default=0,
                   help="0 (default) = no timeout. >0 expires the request after N seconds.")
    s.add_argument("--sentinel-dir", default=None,
                   help="Used with --surface file. Default: ~/Library/Application Support/SAI/receipt-collector/sentinels")
    s.set_defaults(fn=cmd_await_approval)

    s = sub.add_parser("reconcile-billables",
        help="Match expected billables (from plan.json) against QB Purchases "
             "in the window. Surfaces missing tx (paid cash / unknown card) "
             "and extras (in QB but not expected).")
    s.add_argument("--trip", default=None)
    s.add_argument("--plan", required=True,
                   help="plan.json with expected_billables: [{name, txn_date, amount, currency, vendor?}]")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--amount-tol-abs", type=float, default=0.50,
                   help="Absolute amount tolerance (default $0.50).")
    s.add_argument("--amount-tol-pct", type=float, default=0.005,
                   help="Pct amount tolerance (default 0.5%%).")
    s.add_argument("--date-tol-days", type=int, default=2,
                   help="Date tolerance in days (default 2).")
    s.add_argument("--include-all-cards", action="store_true",
                   help="Search every Purchase in the window, not just those on overlay payment_accounts.")
    s.set_defaults(fn=cmd_reconcile_billables)

    s = sub.add_parser("cache-secrets",
        help="One-shot: invoke `op` once to fetch the Anthropic API key, "
             "stash in macOS Keychain so the daemon never has to call `op` "
             "again (no more 'op would like to access data' TCC prompts).")
    s.set_defaults(fn=cmd_cache_secrets)

    s = sub.add_parser("parse-trigger",
        help="DETERMINISTIC rules-tier parser (no LLM). Mostly a fallback for "
             "the LLM agent (use `propose-plan` for normal triggers).")
    s.add_argument("text", help="The trigger text to parse")
    s.set_defaults(fn=cmd_parse_trigger)

    s = sub.add_parser("propose-plan",
        help="PRIMARY trigger entry point: send free-form text to the Haiku "
             "agent which inspects QB + Calendar and stages a plan.json. "
             "Operator approves via await-approval before any QB write.")
    s.add_argument("text", help="The free-form operator message, e.g. "
                                 "'find all receipts for my INSEAD trip May 5-18, 2026'")
    s.set_defaults(fn=cmd_propose_plan)

    s = sub.add_parser("scan-cards")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.set_defaults(fn=cmd_scan_cards)

    s = sub.add_parser("search-receipts")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.set_defaults(fn=cmd_search_receipts)

    s = sub.add_parser("create-purchases")
    s.add_argument("--trip", required=True)
    s.add_argument("--plan", required=True)
    s.set_defaults(fn=cmd_create_purchases)

    s = sub.add_parser("create-invoice")
    s.add_argument("--trip", required=True)
    s.add_argument("--plan", required=True)
    s.add_argument("--currency", default=None,
                   help="Override invoice currency (USD, EUR, etc.). "
                        "If omitted, the plan's invoice_currency wins, then "
                        "the QB customer's currency, then USD.")
    s.set_defaults(fn=cmd_create_invoice)

    s = sub.add_parser("tag-purchases", help="Append 'Billed as expenses to <customer>' to PrivateNote on every "
                                              "Purchase marked for the trip; print a manual tag list for the UI.")
    s.add_argument("--trip", required=True)
    s.add_argument("--customer", required=True, help="Customer display name to write into the memo and the manual-tag list.")
    s.add_argument("--start", required=True, help="Start of the date window (YYYY-MM-DD).")
    s.add_argument("--end", required=True, help="End of the date window (YYYY-MM-DD).")
    s.add_argument("--exclude", default="", help="Comma-separated Purchase IDs to skip (e.g., reclassified non-billable items).")
    s.set_defaults(fn=cmd_tag_purchases)

    s = sub.add_parser("download-receipts", help="Download Gmail receipt threads (body + attachments) to ~/Downloads.")
    s.add_argument("--trip", required=True)
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--out-root", default="~/Downloads", help="Parent folder; output goes into <out-root>/sai-receipts-<trip>/.")
    s.set_defaults(fn=cmd_download_receipts)

    s = sub.add_parser("match-receipts-to-purchases",
        help="Per-Purchase Gmail fetch → render PDF → attach to QB Purchase. Writes index.md mapping receipts ↔ Purchases.")
    s.add_argument("--trip", required=True)
    s.add_argument("--customer", default="", help="Customer name to include in the attachment Note (e.g., your customer's display name).")
    s.add_argument("--start", required=True, help="Date window to look up Purchases (YYYY-MM-DD).")
    s.add_argument("--end", required=True)
    s.add_argument("--out-root", default="~/Downloads")
    s.add_argument("--no-upload", action="store_true",
                   help="Render PDFs to disk but don't upload to QB. Useful for a dry run.")
    s.set_defaults(fn=cmd_match_receipts)

    s = sub.add_parser("attach-onsite-photos",
        help="Find phone-camera receipt photos the operator forwarded to QB's Receipts inbox, match to Purchases, attach as QB Attachables.")
    s.add_argument("--trip", required=True)
    s.add_argument("--customer", default="")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--out-root", default="~/Downloads")
    s.add_argument("--no-upload", action="store_true")
    s.set_defaults(fn=cmd_attach_onsite_photos)

    s = sub.add_parser("gphotos-auth", help="One-time Google Photos OAuth for an account label (e.g., 'lutzT').")
    s.add_argument("--account", required=True, help="Label saved in the token filename. Use 'personal' for your private Google account, 'work' for the work one.")
    s.set_defaults(fn=cmd_gphotos_auth)

    s = sub.add_parser("scan-gphotos", help="Search Google Photos for media items in a date window. Subject to Google partner-only restriction (March 2025).")
    s.add_argument("--account", required=True)
    s.add_argument("--trip", required=True)
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--out-root", default="~/Downloads")
    s.set_defaults(fn=cmd_scan_gphotos)

    s = sub.add_parser("extract-receipt-amounts",
        help="Vision OCR cascade: local Llava first, escalate to Claude Haiku on "
             "low confidence. Advisory; does not write to QB.")
    s.add_argument("--folder", required=True)
    s.add_argument("--model", default=vision_extract.DEFAULT_MODEL,
                   help=f"Cloud model (default {vision_extract.DEFAULT_MODEL}).")
    s.add_argument("--local-model", default=vision_extract.DEFAULT_LOCAL_MODEL,
                   help=f"Local Ollama vision model (default {vision_extract.DEFAULT_LOCAL_MODEL}).")
    s.add_argument("--local-first", action=argparse.BooleanOptionalAction, default=True,
                   help="Cascade order: local Llava first, escalate to cloud on low confidence (default ON; pass --no-local-first to force cloud only).")
    s.set_defaults(fn=cmd_extract_amounts)

    s = sub.add_parser("sense-check",
        help="Cross-check every trip-tagged Purchase against the declared trip window. "
             "Deterministic + local-LLM (Ollama, llama3.2:1b default). Exit code 2 if any NO, 1 if any MAYBE.")
    s.add_argument("--trip", required=True)
    s.add_argument("--customer", required=True)
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--model", default=sense_check.DEFAULT_LOCAL_MODEL,
                   help=f"Ollama model name (default {sense_check.DEFAULT_LOCAL_MODEL}).")
    s.set_defaults(fn=cmd_sense_check)

    s = sub.add_parser("cleanup-pass",
        help="Parse bookkeeping-rules.md and propose (NOT apply) changes "
             "to Purchases in a date window. Writes a markdown proposal doc.")
    s.add_argument("--start", required=True, help="Window start YYYY-MM-DD.")
    s.add_argument("--end", required=True, help="Window end YYYY-MM-DD.")
    s.add_argument("--trip-start", default=None,
                   help="If set, rules mentioning 'trip window' only fire when TxnDate is inside this trip window.")
    s.add_argument("--trip-end", default=None)
    s.add_argument("--out", default=None,
                   help="Output markdown path (default ~/Downloads/cleanup-proposals.md)")
    s.add_argument("--include-all-cards", action="store_true",
                   help="By default we only scan overlay payment_accounts. Set this to scan every Purchase in the window.")
    s.add_argument("--rules", required=True)
    s.set_defaults(fn=cmd_cleanup_pass)

    args = p.parse_args()
    overlay = load_overlay(args.overlay)
    return args.fn(args, overlay)


if __name__ == "__main__":
    sys.exit(main())
