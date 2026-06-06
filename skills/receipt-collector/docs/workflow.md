# cost-compiler — end-to-end workflow

**(Currently shipped under `skills/receipt-collector/`. A rename to
`cost-compiler` is proposed in `docs/PLAN.md` Phase A.)**

This document is the canonical description of how the skill processes a
customer reimbursement trip. It maps the operator's 10-step workflow
onto the SAI base-skill atomic steps, names the runner subcommand for
each step, and flags gaps that are not yet implemented.

It lives in `SAI-baseversion` so it's reusable by any operator. The
operator's specific identifiers (customer Ids, vendor Ids, 1Password
item names, Gmail forward addresses) live in the overlay at
`~/Lutz_Dev/SAI/skills/<skill>/config/identity.yaml`.

---

## Architectural choices this workflow honors

Per SAI `PRINCIPLES.md`, the cost-compiler is built on five durable
choices the operator asked to be made explicit:

1. **Local systems first.** The deterministic rules tier is the
   default. Sense-check (Step 10) runs Ollama `llama3.2:1b` locally
   ($0). The vision tier is the only paid model in the loop and is
   gated to opt-in subcommands.
2. **LLM as fallback, never default.** The cascade only escalates
   when the prior tier abstains. Cloud LLM is the long tail, not the
   front door.
3. **Eval-dataset first.** Every change is gated by canaries
   (hard-fail) + edge_cases (soft-fail) + workflow_regression
   (hard-fail). See `eval/` directory.
4. **Cost control.** Every LLM call writes to
   `~/Library/Logs/SAI/llm_costs.jsonl` with tokens + USD cost.
   Cost cap per call is declared in `skill.yaml` per tier. Daily cap
   is a planned gap (Phase A.4 in `docs/PLAN.md`).
5. **Security.** No secrets in either repo — all references resolve
   to 1Password items at runtime. `OP_SERVICE_ACCOUNT_TOKEN`
   auto-loads from macOS Keychain so no biometric prompt fires
   mid-run. Every QB write goes through the policy gate
   (idempotency marker + approval flag in `outputs:` block of
   `skill.yaml`).

---

## Step 0 — INITIATION

The operator gives the skill a **trip window**, a **customer**, and
(optionally) a **billing currency** (default = USD when omitted). The
operator may also restrict scope ("just airfare and hotel" / "include
on-trip taxis").

Trigger surfaces:

| Surface          | Status       | Atomic step          |
|------------------|--------------|----------------------|
| Claude Code CLI  | ✅ shipped   | `runner.py` argparse |
| Slack DM/channel | ⚠ gap        | TODO `parse_trigger` (Slack adapter) |
| Email to SAI@    | ⚠ gap        | TODO `parse_trigger` (email adapter) |

**Surface continuity invariant:** when triggered from Slack or email,
the skill MUST reply on the SAME surface — review artifacts, approval
asks, and final summary all post back to the originating thread/email.
Never switch channels mid-flow.

---

## Step 1 — COLLECT

Pull every candidate cost the operator might want billed.

| Sub-step | Source                                               | Atomic step                       | Status |
|----------|------------------------------------------------------|-----------------------------------|--------|
| (a) cards | QB credit-card registers (all overlay payment cards) | `scan-cards`                      | ✅ |
| (b) QB-forwards | Gmail forwards to QB Receipts inbox addresses     | `attach-onsite-photos` (search half) | ✅ |
| (c) photos | Google Photos library                              | `scan-gphotos`                    | ⚠ shipped, may 403 (Mar 2025 partner-only restriction) |
| (d) calendar | Pre-booked flights/hotels via Google Calendar    | `infer_trip_window` (window-only) | ⚠ partial — pre-booking extraction not wired |

The base skill carries no card numbers, no inbox addresses, no calendar
IDs. All of those come from the overlay (`payment_accounts`,
`qb_receipts_inboxes`, `gmail_senders`).

---

## Step 2 — REVIEW

Build a list of **billable** vs **NOT billable** items, return it to the
operator on the surface that initiated the run, wait for approval.

| Operation | Status | Atomic step |
|-----------|--------|-------------|
| Build the candidate list | ✅ | `match-receipts-to-purchases` (Gmail) + `attach-onsite-photos` (phone) |
| Render review artifact (final-review.md) | ⚠ partial — `present_review` writes to disk but doesn't auto-post to Slack/email | TODO `post_review_to_surface` |
| Approval gate | ⚠ partial — CLI relies on the operator running the next step manually | TODO `await_approval` |
| Surface continuity (reply where invoked) | ⚠ gap | TODO |

---

## Step 3 — CREATE PDFs

Once the operator approves, render every receipt source to PDF and
drop into `~/Downloads/sai-receipts-<trip>/` (Gmail-sourced) and
`~/Downloads/sai-photos-<trip>/` (photo-sourced).

| Render path         | Atomic step                       | Lib module                |
|---------------------|-----------------------------------|---------------------------|
| Gmail HTML → PDF    | `match-receipts-to-purchases`     | `pdf_render.render_html_pdf` (weasyprint) |
| Gmail text-only → PDF | same                            | `pdf_render.render_text_pdf` (fpdf2) |
| Phone JPEG/HEIC → PDF | `attach-onsite-photos`          | `pdf_render.image_to_pdf` (fpdf2 + Pillow resize) |

Banner on every PDF: `Vendor — Amount Currency / QB Purchase Id=X / date / billed to <Customer> / subject`.

---

## Step 4 — MULTI-CURRENCY

When the customer invoice currency differs from a line's source currency,
look up the **historical FX rate on the date the line was purchased**.
Log it in the audit trail and write it into the line description.

| Operation | Status | Module |
|-----------|--------|--------|
| Static FX fallback (old behavior) | ✅ | `fx.py` |
| Live FX lookup (Frankfurter / ECB) with on-disk cache | ✅ | `fx_live.py` |
| Per-line FX recorded in invoice line description | ✅ (manual call site today) | `lib.invoices.build_invoice_line` |
| Per-line FX written to audit log | ⚠ gap — need a `log_event` call in build_invoice | TODO |

Static `0.92` USD↔EUR fallback was used for the initial &lt;Customer&gt; invoice.
New trips must use `fx_live.get_rate(from, to, on_date)`.

---

## Step 5 — INVOICE

Create the customer Invoice in QB with one line per billable cost,
FX-converted to invoice currency, with PDFs attached.

| Operation | Status | Atomic step |
|-----------|--------|-------------|
| Build invoice object | ✅ | `lib.invoices.build_invoice` |
| Post to QB | ✅ | `create-invoice` subcommand |
| Idempotent re-run (skip on marker match) | ✅ | `[sai-invoice:<trip>]` marker in PrivateNote |
| Attach all per-Purchase receipt PDFs to the Invoice | ✅ | `lib.qb_attachments.upload_for_invoice` (include_on_send=True) |
| Include receipts on customer email | ✅ | `IncludeOnSend=True` in the AttachableRef |

---

## Step 6 — FIND & UPDATE bank transactions in QB

For each billable cost, find the matching bank transaction in QB (the
credit-card download line) and decorate it:

- Add **Tag**: `<customer>` (e.g., `&lt;Customer&gt;`)
- Update **Memo/PrivateNote**: `Billed to <customer> via Invoice #<n>`
- Upload the same receipt PDF as an Attachable

| Sub-step | Status | Notes |
|----------|--------|-------|
| Memo update (`Billed as expenses to <customer>`) | ✅ | `tag-purchases` (idempotent, sparse update) |
| **Tag** write via QB API | ❌ NOT POSSIBLE — Intuit v3 REST has no Tag write endpoint (verified). Step prints a paste-ready list for the QB UI. |
| Upload PDF to the bank transaction | ✅ | `match-receipts-to-purchases` (Gmail-sourced) + `attach-onsite-photos` (photo-sourced) |
| Find matching bank-tx by amount+date when no SAI marker present | ⚠ gap — today we only update Purchases we ourselves created. TODO `find-bank-tx-by-amount-date`. |

The Tag step is the only operator-manual touchpoint and is irreducible
until Intuit ships a public Tag API.

---

## Step 7 — MISSING-TRANSACTION FLAG

If a billable cost doesn't have a matching QB bank-tx, that's either
cash or a card the overlay doesn't know about. Surface it as an error.

| Operation | Status |
|-----------|--------|
| Compare expected billables vs found Purchases | ⚠ gap — TODO `reconcile-billables` |
| Emit JSONL row to audit log + Slack/email | ⚠ gap |

---

## Step 8 — LOGGING

Every step appends one JSONL row to
`~/Library/Logs/SAI/receipt-collector.jsonl`.

| Type of event | Logged? | Where |
|---------------|---------|-------|
| Step start/finish | ✅ | `lib.log.log_event` |
| QB writes (Purchase / Invoice / Attachable created or skipped) | ✅ | called inline by each subcommand |
| LLM calls (cost, tokens, model, step) | ✅ | `lib.llm_costs.log_call` → `~/Library/Logs/SAI/llm_costs.jsonl` |
| FX-rate lookups | ⚠ gap — TODO call `log_event` from `fx_live.get_rate` |

---

## Step 9 — LLM COST CONTROL

Default to deterministic rules. Only call a model when an operator
opts in (`extract-receipt-amounts` subcommand or `--vision` flag).
Prefer the cheapest capable tier (Haiku for vision).

| Operation | Status | Notes |
|-----------|--------|-------|
| Receipt-OCR via Claude vision (Haiku) | ✅ | `lib.vision_extract.extract_receipt`, default model `claude-haiku-4-5` |
| Per-call cost log | ✅ | `lib.llm_costs.log_call` |
| Daily cost rollup | ✅ | `lib.llm_costs.today_usd_total` printed after every batch |
| Local LLM (Ollama / llama.cpp) first, then Claude on low-confidence | ⚠ gap — TODO `local_vision_extract` (Llava-style local model), escalate to Haiku when `confidence == "low"` |
| Budget cap with hard stop | ⚠ gap — TODO `LLM_DAILY_USD_CAP` in overlay |

---

## Step 10 — SENSE CHECK (final gate before handoff)

Before the customer invoice is handed off (in execution order this
runs BEFORE Step 5 / Invoice creation), every Purchase tagged for the
trip is re-checked against the declared trip window. Catches the
`a rideshare 2026-04-09 → <Customer> May 2026` class of mis-tag that
previously slipped through (the Lyft was 26 days outside the window).

Two-tier (local-LLM-first):

| Gate | Cost | Verdict source |
|------|------|----------------|
| Deterministic date+vendor check | free | inside window → YES; outside but matches airline/hotel pre-booking heuristic → YES; outside by >30 d → NO; ambiguous → MAYBE |
| Local LLM (Ollama `llama3.2:1b` default) | ≈$0, sub-second, fully local | only runs on MAYBE verdicts; returns YES/MAYBE/NO with one-line reason |

Subcommand: `sense-check --trip --customer --start --end [--model llama3.2:1b]`
Exit code: 0=all clear, 1=some MAYBE, 2=some NO.

Audit log row written every run (verdict + reason per Purchase). The
local-LLM-first principle from Step 9 applies here — this gate never
calls a paid model.

---

## Execution order (vs concern order)

The 10 steps above are numbered as **concerns** the operator wants
addressed. Execution order is slightly different — sense-check (#10
"final gate") runs BEFORE invoice creation (#5):

```
Step 0 — INITIATION
Step 1 — COLLECT
Step 2 — REVIEW (operator approval gate)
Step 3 — CREATE PDFs
Step 4 — MULTI-CURRENCY (FX lookup, log rates)
Step 10 — SENSE CHECK (final-gate sanity check)
Step 5 — INVOICE
Step 6 — FIND & UPDATE bank tx (tag, memo, attach)
Step 7 — MISSING-TRANSACTION FLAG
Step 8 — LOGGING (cross-cutting, runs at every step)
Step 9 — LLM COST CONTROL (cross-cutting, runs at every LLM call)
```

---

## Cleanup pass (post-invoice)

After the invoice is sent, the operator often leaves rules behind
("food while traveling is not reimbursable but IS a travel-cost
expense"). The `cleanup-pass` subcommand applies these rules in a
later run.

| Operation | Status |
|-----------|--------|
| Rules file persisted under overlay | ✅ `bookkeeping-rules.md` |
| Read rules and walk recent Purchases | ⚠ partial — `cleanup-pass` is a stub that prints rules but doesn't apply them yet |

---

## SAI architecture compliance checklist

✅ **Base / overlay split**
- Capabilities (QB read, Gmail read, PDF render, vision OCR, 1P shim) live in `SAI-baseversion/skills/receipt-collector/lib/`.
- Operator data (customer Ids, vendor map, payment-account map, gmail senders, FX rate table, QB receipts inbox addresses, bookkeeping rules) lives in `SAI/skills/receipt-collector/config/identity.yaml` + `bookkeeping-rules.md`.
- The base skill carries zero operator-specific values.

✅ **No secrets outside 1Password**
- QB OAuth creds: 1Password item `&lt;qb-1password-item&gt;` (vault `&lt;vault&gt;`), read at runtime via `op` CLI.
- Anthropic API key: 1Password item `&lt;anthropic-1password-item&gt;` (vault `&lt;vault&gt;`), read at runtime.
- 1Password Service Account token: auto-loaded into `OP_SERVICE_ACCOUNT_TOKEN` via macOS Keychain + `~/.zshenv` (no biometric prompts mid-task).
- Google OAuth refresh tokens: `~/.SAI/gmail_token.json`, `~/.SAI/gphotos_token_<label>.json` — these are short-lived refresh tokens, not service accounts.

✅ **Everything logged**
- Per-step events: `~/Library/Logs/SAI/receipt-collector.jsonl`
- LLM-call costs: `~/Library/Logs/SAI/llm_costs.jsonl`
- FX rate cache (audit + reuse): `~/Library/Caches/SAI/fx_rates.json`
- Per-trip artifacts: `~/Lutz_Dev/SAI/skills/receipt-collector/trip_runs/<slug>/`

⚠ **Small-LLM-first**
- Vision: Claude Haiku 4.5 (cheapest tier) — ✅
- No usage of larger models in the hot path — ✅
- Local-LLM fallback (Ollama/llama.cpp) — ⚠ not yet wired
- Daily $ cap — ⚠ not yet wired

✅ **Atomic, reusable steps**
- Every step is a separate runner subcommand and can be invoked
  independently:
  `check-auth`, `scan-cards`, `search-receipts`, `create-purchases`,
  `create-invoice`, `tag-purchases`, `download-receipts`,
  `match-receipts-to-purchases`, `attach-onsite-photos`,
  `gphotos-auth`, `scan-gphotos`, `extract-receipt-amounts`,
  `cleanup-pass`.

✅ **No personal data in SAI-baseversion**
- `skill.yaml` schema only.
- `lib/*.py` have no hard-coded IDs.
- `canaries.jsonl`, `edge_cases.jsonl`, `workflow_regression.jsonl`
  use synthetic data.

---

## Gaps to close (priority order)

1. **Slack/email triggers + reply-on-same-surface** — biggest UX gap.
2. **Approval gate** that pauses between step 2 and step 3.
3. **Reconciliation** (steps 6+7) — search bank-tx by amount/date, flag misses.
4. **Local-LLM fallback** for vision OCR with confidence-based escalation.
5. **FX-rate audit log entry** — each invoice line should write the rate into the audit JSONL.
6. **Daily LLM budget cap** with hard stop.
7. **Calendar pre-booking extraction** for flights/hotels paid weeks ahead.
