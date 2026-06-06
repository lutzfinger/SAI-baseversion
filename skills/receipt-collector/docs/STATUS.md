# receipt-collector — session recap and forward plan

**As-of:** 2026-05-23 evening (daemon hardened with watchdog)

## Latest update — daemon resilience (2026-05-23)

The email-listen daemon (`com.sai.receipt-collector.email-listen`) is
now monitored by a sibling LaunchAgent
(`com.sai.receipt-collector.email-listen-watchdog`) that fires every
5 minutes and re-loads the daemon if `launchctl list` no longer shows
it. This closes the gap that bit us on 2026-05-23: the daemon had
been unloaded mid-session (cause unclear — likely repeated-crash
auto-removal by launchd), and stayed gone because `KeepAlive` only
covers process death, not whole-job removal.

Layered recovery:

| Layer | Handles | Mechanism |
|---|---|---|
| `KeepAlive: true` in main plist | Daemon process crash | launchd restarts process |
| Watchdog LaunchAgent | Whole job unloaded | Re-runs `launchctl load` every 5 min |
| `~/Library/LaunchAgents` location | Reboot / re-login | Both auto-load at login |

Files added:
- `~/Lutz_Dev/SAI/skills/receipt-collector/scripts/email-listen-watchdog.sh`
- `~/Library/LaunchAgents/com.sai.receipt-collector.email-listen-watchdog.plist`

Watchdog log: `~/Library/Logs/SAI/email-listen-watchdog.log`
(silent unless a reload actually happens).

## sai@ as a multi-workflow front door

The shared `sai@lutzfinger.com` inbox is now a full assistant
interface. Every incoming email goes through `lib/dispatch_agent.py`
(rules tier → Haiku tier) and is classified into one of 5 verdicts:

| Verdict | What happens |
|---|---|
| `COST_COMPILER` | Existing flow: open intent → `cost_compiler_agent` → stage plan → collect phase → AWAITING_APPROVAL → invoice |
| `EVAL_FEEDBACK` | Log to `eval_feedback_inbox.jsonl` + send a brief acknowledgment reply ("Got it — I logged your label correction.") |
| `GENERAL_QUERY` | Invoke `lib/general_assistant.respond_to_query` — Claude with `web_search_20250305` server tool. Jokes, research, opinions, code help. |
| `WORKFLOW_SUGGESTION` | Claude writes a structured "here's what it could look like" proposal. **NEVER creates anything** — per SAI #9 email is not a registered edit channel. |
| `IGNORE` | Pure noise (boarding passes, vendor newsletters). Marked read, audited, NO reply. |

All replies route through `lib/email_format.py` for human-readable
tone (conversational summary at top, tech details collapsed at the
bottom). The prior "logfile-dump-by-email" experience is gone.

Files added/changed this session:
- `lib/dispatch_agent.py` (NEW; replaces `lib/intent_router.py`)
- `lib/general_assistant.py` (NEW — Claude+web_search interface)
- `lib/email_format.py` (NEW — conversational formatter)
- `lib/email_runner.py` (rewired to use dispatch_agent + email_format)
- `prompts/cost_compiler_agent.md` (re-propose rule loosened)

Total LLM spend across all of today's testing: **~$0.30** of $5 cap.

---
**Author:** built collaboratively with Claude in Auto Mode
**Purpose:** single document to resume work from after a session compact.

**Companion docs:**
- `docs/workflow.md` — canonical 10-step workflow description
- `docs/PLAN.md` — phased gap-closure plan (A → B → C → D)
- `docs/DECISIONS.md` — log of non-obvious choices during execution

---

## What landed in 2026-05-20 session 2 (Phases A-D)

**Phase A — Quick wins:**
- A.2 ✅ `fx_live` wired into `create-invoice`. Each line carries its
  conversion: live ECB rate → static fallback → fail-closed. Every
  lookup writes an `fx_lookup` audit row.
- A.3 ✅ `--currency EUR|USD|...` CLI override + new `parse-trigger`
  subcommand that extracts {customer, trip-slug, currency,
  month-year, scope} from free-form text.
- A.4 ✅ Daily LLM cost cap of $5.00/day (overlay
  `policy.daily_llm_cap_usd`). `BudgetExceeded` raises BEFORE the API
  call. Wired into vision-extract.
- A.5 ✅ Boundary linter: 515 files scanned, 0 violations.

**Phase B — Workflow completeness:**
- B.1 ✅ `reconcile-billables` subcommand matches expected billables
  to QB Purchases. Surfaces missing tx (paid cash / unknown card) +
  extras (in QB but not expected). Tolerance: amount ±max($0.50,
  0.5%), date ±2d.
- B.2 ✅ `await-approval` primitive with durable JSONL state. Surfaces:
  `cli` (stdin), `file` (sentinel-file). Reply parser handles
  multi-word phrases ("looks good"), feedback ("um wait") stays OPEN
  per #16g.
- B.3 ✅ `extract-pre-bookings` reads Google Calendar (new
  `lib/calendar_fetch.py`) and surfaces flights/hotels booked weeks
  before the trip start. Destination-hint filter downgrades
  cross-trip noise.
- B.4 ✅ `cleanup-pass` now PARSES `bookkeeping-rules.md` and PROPOSES
  changes (per #20 reflection-may-suggest). Writes a markdown
  proposal doc; no QB writes.
- B.5 ✅ +6 canaries / edge_cases / workflow_regression entries
  covering reconcile, approval, cleanup, pre-booking, FX, daily-cap.

**Phase C — Surface triggers:**
- C.1 ✅ `parse_trigger.derive_plan(req)` maps a TripRequest into a
  deterministic ordered list of subcommands — same plan used by CLI,
  Slack, email.
- C.2 ✅ `slack-listen` subcommand. Long-poll Slack
  `conversations.history`, parse triggers, drive the plan with
  status replies in the same thread. Token via 1Password.
- C.3 ✅ `email-listen` subcommand. Poll Gmail for
  `label:sai-trigger is:unread from:<operator>`, drive the plan,
  reply on the same thread. Uses a SEPARATE OAuth token
  (`gmail_send_token.json`) so the read-only fetcher doesn't
  inherit send scope.
- C.4 ✅ Surface-continuity invariant locked in
  `workflow_regression.jsonl` (wf-08-slack, wf-09-email).

**Phase D — Local-LLM-first vision:**
- D.1 ✅ Llava local pass first via Ollama, escalate to Haiku on low
  confidence or null total. `--local-first` defaults ON; the cascade
  transparently degrades to cloud-only when Llava isn't pulled.
- D.2 ✅ 2 new hard-fail canaries (`canary-06`, `canary-07`) lock the
  cascade order: local first ALWAYS runs; cloud ONLY on escalation.

**SAI compliance after Phase A-D:**
- 0 boundary-linter violations.
- 0 new operator-specific values in base.
- Overlay gained: `policy.daily_llm_cap_usd: 5.00` (and future
  `slack.*` / `email.*` blocks for Phase C deployment).
- Eval datasets: canaries=7, edge_cases=15, workflow=10. All synthetic.

---

## TL;DR

A 14-subcommand SAI skill that takes a trip window + customer name and:
1. pulls every billable expense (QB cards, Gmail forwards, phone-photo
   forwards, calendar pre-bookings),
2. renders each receipt to PDF (vendor-branded via weasyprint, photos
   via fpdf2+Pillow),
3. builds a customer Invoice in QB with FX-converted lines,
4. attaches every receipt PDF to both the Purchases and the Invoice,
5. sense-checks every Purchase against the trip window using a free
   local LLM (Ollama `llama3.2:1b`) before invoicing.

The first end-to-end trip (`insead-2026-05`, customer INSEAD, EUR) is
fully reconciled in QB: Invoice 2296 totals €7,320.74 with 9 receipt
PDFs attached. Today's LLM spend: $0.034.

---

## Current state of the live INSEAD trip

**Invoice 2296** — https://qbo.intuit.com/app/invoice?txnId=2296
Customer: INSEAD · Currency: EUR · Total: **€7,320.74** · Lines: 5

| Purchase | Date | Amount | Vendor | PDF attached |
|---|---|---|---|---|
| [2290](https://qbo.intuit.com/app/expense?txnId=2290) | 2026-05-05 | $56.44 | Uber MV→SFO | ✅ Gmail receipt |
| [2291](https://qbo.intuit.com/app/expense?txnId=2291) | 2026-05-05 | $6,343.93 | United BR2HFN | ✅ Gmail (3 re-issues) |
| [2292](https://qbo.intuit.com/app/expense?txnId=2292) | 2026-05-06 | €168.00 | Taxi CDG→Ermitage | ✅ phone photo |
| [2293](https://qbo.intuit.com/app/expense?txnId=2293) | 2026-05-15 | €1,072.40 | Hotel Ermitage (food-excluded) | ✅ phone photos (×2) |
| [2294](https://qbo.intuit.com/app/expense?txnId=2294) | 2026-05-16 | €192.00 | Taxi Ermitage→CDG | ✅ phone photos (×2) |

**Purchase 2297** (Lyft April 9 with William, $55.58) — **reclassified to
Cornell trip**, removed from INSEAD invoice. Memo carries
`[sai-reclassified]` reason. Caught by sense-check (test confirmed).

**Final step still owed by operator:** manually add `INSEAD` tag in
QB UI to the 5 Purchases (the v3 REST API has no Tag write endpoint —
verified twice). Then review Invoice 2296 and click Send.

---

## The 14 atomic steps

All in `~/Lutz_Dev/SAI-baseversion/skills/receipt-collector/runner.py`,
independently invocable.

| # | Subcommand | What it does |
|---|---|---|
| 1 | `check-auth` | QB OAuth round-trip via 1P → confirms company connection |
| 2 | `scan-cards` | Lists all Purchases in window across every overlay payment card |
| 3 | `search-receipts` | Builds the Gmail query string (no fetch) |
| 4 | `create-purchases` | Posts new QB Purchases from a plan.json (idempotent marker) |
| 5 | `create-invoice` | Posts a customer Invoice (idempotent marker) |
| 6 | `tag-purchases` | Appends "Billed to <customer>" to PrivateNote + prints paste-ready list for the QB UI tag step |
| 7 | `download-receipts` | Broad Gmail sweep → flat PDF folder in ~/Downloads |
| 8 | `match-receipts-to-purchases` | Per-Purchase Gmail fetch + PDF render + QB Attachable upload |
| 9 | `attach-onsite-photos` | Gmail forwards to QB receipts inboxes → phone photo PDFs → QB Attachables |
| 10 | `gphotos-auth` | OAuth grant per Google account label (e.g., `personal`) |
| 11 | `scan-gphotos` | Search Google Photos library by date window |
| 12 | `extract-receipt-amounts` | Claude Haiku vision on a folder; prints structured (total, vendor, date) + cost |
| 13 | `sense-check` | Deterministic + local-LLM date plausibility per Purchase |
| 14 | `cleanup-pass` | Parse `bookkeeping-rules.md` and PROPOSE changes (Phase B.4); writes a markdown proposal doc |
| 15 | `parse-trigger` | Extract {customer, trip slug, currency, month, scope} from free-form initiation text (Phase A.3) |
| 16 | `reconcile-billables` | Match expected billables to QB Purchases; flag missing tx / extras (Phase B.1; covers steps 6+7) |
| 17 | `await-approval` | Durable operator-approval gate. Surfaces: `cli` (stdin), `file` (sentinel) (Phase B.2) |
| 18 | `extract-pre-bookings` | Find flights/hotels booked weeks before the trip (Phase B.3) |
| 19 | `slack-listen` | Long-poll Slack channel for triggers; status replies on same thread (Phase C.2) |
| 20 | `email-listen` | Poll Gmail for trigger emails; status replies on same thread (Phase C.3) |
| 21 | `propose-plan` | **PRIMARY trigger entry.** Haiku agent with guarded tool surface inspects QB + Calendar and stages a plan.json. Replaces regex parsing per 2026-05-20 decision. |

---

## Lutz's 10-step target workflow — implementation status

| Step | Target | Status | Notes |
|---|---|---|---|
| 0 | Slack / email / Claude Code trigger | ✅ scaffolded + **LLM agent** | `propose-plan` runs Claude Haiku with a guarded tool surface (mirroring `app/agents/sai_eval_agent.py`). Tested end-to-end: ambiguous trigger → agent asked which of 2 INSEAD customers ($0.0055, 2 iter); disambiguated trigger → agent staged plan.json ($0.010, 3 iter); off-topic → agent declined politely ($0.0027, 1 iter). `parse_trigger.parse` kept as rules-tier fallback for LLM-unreachable case (#29). |
| 0 | Currency override (default USD) | ✅ | Agent honors customer's QB default currency; operator can override in trigger ("bill in EUR"); rules-tier fallback defaults to USD. |
| 1a | QB credit-card scan (all cards) | ✅ | `scan-cards` |
| 1b | Gmail forwards to QB inboxes | ✅ | `attach-onsite-photos` |
| 1c | Google Photos for receipt photos | ⚠ Lib ships, blocked on API | March-2025 Google partner-only restriction may 403; operator manual setup steps documented |
| 1d | Calendar pre-booked flights/hotels | ✅ (Phase B.3) | `extract-pre-bookings` subcommand. Reads Google Calendar, classifies flight/hotel/unknown, filters by destination hints. Operator review before billing. |
| 2 | REVIEW + approval gate | ✅ (Phase B.2) | `await-approval` primitive with durable JSONL state. Surfaces: cli, file. Multi-word reply parser ("looks good" → APPROVED); unrecognised replies stay OPEN per #16g. |
| 3 | Create PDFs in Downloads | ✅ | weasyprint (HTML w/ images) + fpdf2 (text + JPEG wrap with EXIF rotate) |
| 4 | Live FX rate on date-of-purchase + log | ✅ (Phase A.2) | `lib/fx_live.py` (Frankfurter/ECB, on-disk cache) wired into `build_invoice_line`; each lookup writes one `fx_lookup` row to audit log with rate + source ("live" / "static" / "identity"); falls back to overlay's `fx.default_table` when live fails; raises ValueError when neither — fail-closed |
| 5 | INVOICE with FX + attached PDFs | ✅ | All 9 PDFs on Invoice 2296 with IncludeOnSend=true |
| 6 | Find + tag + memo + attach bank tx | ✅ (Phase B.1) | `reconcile-billables` matches expected vs found Purchases with configurable tolerance. Tag column still manual UI (no QB API). |
| 7 | Flag missing bank tx (cash / other card) | ✅ (Phase B.1) | `reconcile-billables` surfaces missing tx as audit-log warnings and exits non-zero so callers can hard-stop. |
| 8 | Log every event | ✅ | `~/Library/Logs/SAI/receipt-collector.jsonl` — `fx_lookup`, `reconcile_billables`, `approval_open/close`, `extract_pre_bookings`, `cleanup_pass`, `budget_exceeded`, `slack_listen_*`, `email_listen_*` events |
| 9 | LLM cost reporting + local-LLM-first | ✅ + ✅ daily cap (A.4) + ✅ Llava cascade (D.1) | Vision now runs Llava local first, escalates to Haiku on low confidence. Local-tier hits log $0 cost rows so dashboards can track cascade hit-rate. Daily cap of $5.00/day fires BEFORE the API call. |
| 10 | Sense-check before invoice handoff | ✅ | `sense-check` subcommand; deterministic + Ollama llama3.2:1b |

---

## SAI architecture compliance — verified clean

**Base / overlay split.** Six categories audited, all clean in base after
the cleanup of session 2026-05-20:

| Category | Base | Overlay |
|---|---|---|
| Credit card numbers (7925, 9317) + brand names | ✅ none | `payment_accounts.*.last4` |
| Customer / vendor / driver / confirmation numbers | ✅ none | `default_customer.id`, `vendors.*.id`, etc. |
| QB realm / account / vendor IDs | ✅ none | `expense_accounts.*.id`, `vendors.*.id`, `default_customer.id`, `secrets.qb.fields.realm_id` |
| Email addresses + 1Password item names | ✅ none | `secrets.qb.op_item`, `secrets.anthropic.op_item`, `qb_receipts_inboxes` |
| Specific airline / hotel / rideshare brand strings in functional code | ✅ none | `sense_check.airline_hints`, `sense_check.hotel_hints`, `receipt_match.*` maps, `gmail_sender_to_vendor`, `calendar.travel_keywords` |
| Gmail thread IDs / Attachable IDs from real trips | ✅ none (eval data uses synthetic ACME / Rideshare A/B / Lodging Provider) | per-trip artifacts in `trip_runs/<slug>/` |

**Secrets in 1Password only.**
- QB OAuth: item `SAI-Intuit-Prod` in vault `SAI-Key`
- Anthropic API key: item `Anthropic - SAI - Admin Key` in vault `SAI-Key`
- 1Password Service Account token: auto-loaded into `OP_SERVICE_ACCOUNT_TOKEN`
  by `~/.zshenv` from macOS Keychain entry `sai-op-service-account-token`,
  so no biometric prompts during a run.
- Google OAuth refresh tokens (short-lived, not service accounts):
  `~/.SAI/gmail_token.json`, `~/.SAI/gphotos_token_<label>.json` — mode 0600.

**Logging.**
- Audit events: `~/Library/Logs/SAI/receipt-collector.jsonl`
- LLM-call cost log: `~/Library/Logs/SAI/llm_costs.jsonl`
- FX-rate cache (reuse + audit): `~/Library/Caches/SAI/fx_rates.json`
- Per-trip artifacts: `~/Lutz_Dev/SAI/skills/receipt-collector/trip_runs/<slug>/`

**Small-LLM-first.** Sense-check uses fully local Ollama llama3.2:1b ($0).
Vision uses Claude Haiku 4.5 (cheapest paid vision tier; $0.0033/photo).
Total receipt-collector spend today: $0.034.

---

## File map (base skill)

```
~/Lutz_Dev/SAI-baseversion/skills/receipt-collector/
├── runner.py                       — 14 subcommands
├── skill.yaml                      — manifest, 17 tiers
├── README.md                       — capability overview
├── canaries.jsonl                  — 3 hard-fail tests (synthetic)
├── edge_cases.jsonl                — 9 soft-fail tests (synthetic; includes wf-04 sense-check regression)
├── workflow_regression.jsonl       — 4 end-to-end tests (synthetic)
├── docs/
│   ├── workflow.md                 — canonical workflow doc with SAI compliance checklist
│   └── STATUS.md                   — this file
└── lib/
    ├── qb_client.py                — QB OAuth + REST client; 1P refresh-token rotation
    ├── qb_attachments.py           — multipart upload to /v3/.../upload, any entity
    ├── qb_tags.py                  — memo updates + manual-tag report
    ├── op_secrets.py               — 1Password CLI wrapper
    ├── gmail_search.py             — generic query + canonical_vendor (overlay-driven)
    ├── gmail_fetch.py              — Gmail thread fetch + attachment download
    ├── calendar_fetch.py           — Google Calendar read (Phase B.3); reuses ~/.SAI/credentials.json + per-skill token
    ├── google_photos.py            — Photos OAuth + search + download
    ├── purchases.py                — Purchase JSON body builder
    ├── invoices.py                 — Invoice JSON body builder; on_fx_log + fx_fallback_table (Phase A.2)
    ├── pdf_render.py               — weasyprint (HTML+images) + fpdf2 (text + image wrap)
    ├── forwarded_receipts.py       — match Gmail forwards to QB receipts inboxes against Purchases
    ├── receipt_match.py            — per-Purchase Gmail query derivation (overlay-driven vendor maps)
    ├── trip_calendar.py            — calendar trip-window inference + extract_pre_bookings() (Phase B.3)
    ├── sense_check.py              — deterministic + Ollama llama3.2:1b plausibility gate
    ├── vision_extract.py           — cascade: Llava local → Haiku cloud (Phase D.1); overlay-driven 1P refs
    ├── parse_trigger.py            — DETERMINISTIC fallback parser (kept for LLM-unreachable case; #29). Primary trigger interpreter is cost_compiler_agent.
    ├── cost_compiler_agent.py      — **PRIMARY trigger interpreter.** Claude Haiku tool-use loop with iteration cap + audit. Mirrors slack-eval agent shape.
    ├── cost_compiler_tools.py      — Tool surface for the agent (list_qb_customers, search_calendar_events, list_payment_accounts, list_expense_accounts, propose_plan).
    ├── reconcile.py                — match expected billables to QB Purchases (Phase B.1)
    ├── approval.py                 — durable JSONL approval gate with cli + file surfaces (Phase B.2)
    ├── cleanup.py                  — parse bookkeeping-rules.md + propose changes (Phase B.4)
    ├── slack_runner.py             — Slack trigger surface (Phase C.2); requires slack-sdk + 1P bot token
    ├── email_runner.py             — Gmail trigger surface (Phase C.3); separate gmail.send token
    ├── llm_costs.py                — per-call cost log + daily rollup + BudgetExceeded (Phase A.4)
    ├── fx_live.py                  — Frankfurter (ECB) live FX with disk cache
    ├── fx.py                       — static FX fallback (legacy; kept for completeness)
    └── log.py                      — JSONL audit logger
```

## File map (operator overlay)

```
~/Lutz_Dev/SAI/skills/receipt-collector/
├── README.md
├── PRD.md
├── bookkeeping-rules.md
├── intuit-production-form-cheatsheet.md
├── config/
│   ├── identity.yaml               — all operator config (secrets refs, payment_accounts,
│   │                                  expense_accounts, vendors, invoice_items,
│   │                                  gmail_senders, gmail_sender_to_vendor,
│   │                                  qb_receipts_inboxes, sense_check.{airline,hotel}_hints,
│   │                                  receipt_match.{on_site_vendor_hints,airline_vendor_to_sender,
│   │                                                 rideshare_vendor_to_sender,airport_codes_to_ignore},
│   │                                  calendar.travel_keywords, fx.default_table)
│   └── 1password-refs.yaml         — pointer to QB OAuth item (no secrets)
└── trip_runs/
    └── insead-2026-05/
        ├── dates.md
        ├── receipts.csv
        ├── final-review.md
        ├── run.log
        └── scripts/                — one-shot trip scripts (now superseded by atomic steps)
```

---

## Gaps to close — priority order (after Phase A-D)

### Operator deployment (no code work; manual setup only)

1. **Slack workspace setup for `slack-listen`.** Operator creates a
   Slack app, scopes `chat:write` + `channels:history` + `reactions:read`,
   stores the bot token in 1Password, fills in overlay's
   `slack.{bot_token_op_ref, channel_id, operator_user_id}`. Then
   `python -m skills.receipt-collector.runner slack-listen`.
2. **Gmail trigger setup for `email-listen`.** Operator creates a
   Gmail label `sai-trigger`, runs the OAuth flow once for
   `gmail.send` scope (writes `~/.SAI/gmail_send_token.json`), fills
   in overlay's `email.{trigger_label, from_address, operator_email}`.
   Then `python -m skills.receipt-collector.runner email-listen`.
3. **Pull `llava:7b` for free local vision.** `ollama pull llava:7b`
   (~4.7 GB). Without it the cascade transparently falls back to
   Haiku — so this is optional but cuts vision spend to $0.
4. **Google Photos OAuth for `lutzTfinger@gmail.com`.** Operator
   manual steps documented in chat. Subject to March-2025 partner-only
   restriction; the `attach-onsite-photos` (Gmail-forward) path is the
   primary workaround.

### Code work (deferred to Phase E+)

5. **Cleanup-pass APPLY step** (currently propose-only per #20).
   Add `apply-rule --rule R1 --confirm` subcommand that takes the
   proposal markdown and writes the QB updates one row at a time
   with operator confirmation. ~2 hours.
6. **Migrate sense-check + vision prompts to hash-locked files** per
   #24c. Today they're inline string constants. ~1 hour.
7. **Opt-in `true_north` dataset** per #16h once the skill has been
   live for 30+ days. ~30 min once we hit that point.
8. **Graduation experiment** comparing Llava local % vs Haiku at 20%
   sample (per #15). Auto-tuned cascade threshold.
9. **LangSmith trace wiring** — declared in `skill.yaml`
   (`observability.langsmith_project: SAI`) but not yet emitting.
   ~30 min.

---

## Resume notes for next session

When you next open this skill:

1. Read this STATUS.md first — it's the canonical recap.
2. `docs/workflow.md` is the canonical workflow doc with the 10-step
   target → atomic-step mapping.
3. The overlay's `config/identity.yaml` carries every operator-specific
   value; the base skill carries none.
4. Today's spend so far: `python3 -c "import sys; sys.path.insert(0,'skills/receipt-collector'); from lib.llm_costs import today_usd_total; print(today_usd_total('receipt-collector'))"`
5. To re-run sense-check on any trip: `python -m skills.receipt-collector.runner sense-check --trip <slug> --customer <name> --start <YYYY-MM-DD> --end <YYYY-MM-DD>`
6. The QB Tag step **must** be done manually in the QB UI (Intuit v3
   REST has no Tag write endpoint, verified twice). `tag-purchases`
   prints a paste-ready list of Purchase Ids to make the click-through
   fast.
7. If you regenerate PDFs and find Chrome timing out, *that is expected*
   on this Mac — we abandoned Chrome headless and switched to
   weasyprint (HTML, images) + fpdf2 (text + image wrap). Do not retry
   Chrome.
8. Photos Library API is partner-only since March 2025. Don't spend
   more cycles trying to make it work for personal accounts; rely on
   email-forward path (`attach-onsite-photos`) which works.
