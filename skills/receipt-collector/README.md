# receipt-collector (base skill)

Collect business-trip travel receipts from multiple sources (Gmail receipts, QuickBooks credit-card registers, Receipts inbox forwards) and assemble a customer reimbursement invoice in QuickBooks Online.

The base skill is **operator-agnostic** — no hard-coded vendor IDs, account IDs, customer names, or credentials. An operator overlay (e.g., `~/Lutz_Dev/SAI/skills/receipt-collector/`) supplies all personal mappings via `config/identity.yaml` + 1Password references.

## Architecture

```
operator trigger (email/Slack)
        ▼
[parse_trigger]              ─►  {trip_slug, customer_hint, requested_categories}
[infer_trip_window]          ─►  {start, end, supporting_events}        ← Google Calendar
[confirm_trip_window]        ─►  operator approval gate
[infer_customer]             ─►  {customer_id, currency}                ← QB Customer match
[search_email_receipts]      ─►  list of receipt threads               ← Gmail
[scan_credit_card_registers] ─►  list of QB Purchases in window         ← QB (every payment account)
[extract_amounts]            ─►  amounts (+ placeholder flag for image-only)
[match_to_receipts_list]     ─►  matched / unmatched / extras
[create_purchases]           ─►  Purchase IDs in QB (idempotent)
[build_customer_invoice]     ─►  invoice object (multi-line, FX-converted)
[create_invoice]             ─►  Invoice ID in QB
[present_review]             ─►  final-review.md for operator
[capture_bookkeeping_rules]  ─►  optional operator input
[cleanup_pass] (deferred)    ─►  apply rules to recent Purchases
```

Every step is **atomic** — runnable independently via the runner's subcommands, so partial-pipeline runs and tests don't need to fake earlier steps.

No LLM in the hot path. All tiers are deterministic rules.

## Subcommands

```bash
python -m skills.receipt-collector.runner check-auth
python -m skills.receipt-collector.runner scan-cards         --start 2026-05-05 --end 2026-05-18
python -m skills.receipt-collector.runner search-receipts    --start 2026-05-05 --end 2026-05-18
python -m skills.receipt-collector.runner create-purchases   --trip <slug> --plan plan.json
python -m skills.receipt-collector.runner create-invoice     --trip <slug> --plan plan.json
python -m skills.receipt-collector.runner tag-purchases      --trip <slug> --customer "<your-customer>" --start 2026-05-05 --end 2026-05-18
python -m skills.receipt-collector.runner download-receipts  --trip <slug> --start 2026-05-05 --end 2026-05-18
python -m skills.receipt-collector.runner match-receipts-to-purchases --trip <slug> --start 2026-01-01 --end 2026-06-30
python -m skills.receipt-collector.runner cleanup-pass       --rules ~/path/bookkeeping-rules.md
```

### tag-purchases — when the customer cannot be on each Purchase

QB Essentials (and lower) does not let a Purchase carry `BillableStatus +
CustomerRef`. The workaround is to keep the Purchase plain, send a
separate Invoice in the customer's currency, and on each Purchase: add
the customer name as a **Tag** plus a "Billed as expenses to &lt;customer&gt;"
memo. The Tag side has no public v3 API, so `tag-purchases`:

1. finds every Purchase carrying the `[sai-receipts:<trip>]` marker in
   PrivateNote,
2. appends `Billed as expenses to <customer>` to PrivateNote (idempotent),
3. prints a list of Purchase IDs with date + amount + vendor so the
   operator can paste the Tag into the QB UI in one batch.

### download-receipts — pack receipt PDFs/images for the customer

For each Gmail thread that matches the trip window and the overlay's
`gmail_senders`, save `<thread_id>/body.txt`, `subject.txt`, and every
attachment to `~/Downloads/sai-receipts-<trip>/`. The operator then
attaches that folder to the reimbursement email.

First run prompts a Google OAuth flow that adds the Gmail read scope and
writes the token to `~/.SAI/gmail_token.json` (mode 0600).

### match-receipts-to-purchases — targeted per-Purchase fetch + PDF + QB attach

`download-receipts` does a date-window sweep, which is fine for ride-share
and on-trip purchases but misses receipts whose booking predates the trip
(flights bought months ahead). `match-receipts-to-purchases` is the
flagship per-Purchase pipeline:

1. For each Purchase carrying the trip marker, derive a precise Gmail
   query from its memo (confirmation number for airlines, driver name
   for Lyft, date+vendor for Uber).
2. Download every matching thread (body.txt + body.html) into
   `<out-root>/sai-receipts-<trip>/purchase-<id>/<thread_id>/`.
3. Render the text body to a PDF via `fpdf2` (pure Python, no Chrome —
   tried Chrome headless first, but it consistently took 60-120s per page
   on Gmail HTML even with network short-circuited; the text path is
   <0.2s and preserves all the receipt data customers actually read).
4. Upload the PDF to QB as an Attachable linked to that Purchase
   (`POST /v3/company/<realm>/upload`, idempotent via a marker in the
   Attachable.Note field).

Vendors paid on site (taxi, hotel front desk) get
`receipt_status="no_email_receipt_expected"` — operator attaches a phone
photo manually.

The QB **Tags** column still requires a UI step — Intuit's v3 REST API
has no Tag write endpoint (verified: both `SELECT * FROM Tag` and
`/v3/.../tag` return "Unsupported Operation"). Use `tag-purchases` first;
it prints a paste-ready list of Purchase Ids for the manual tag step.

The plan.json format is documented in `docs/plan-schema.md` (TBD).

## Files

- `runner.py` — subcommand dispatcher
- `skill.yaml` — manifest (atomic tiers, eval datasets, policy)
- `lib/op_secrets.py` — 1Password CLI wrapper
- `lib/qb_client.py` — QBO REST client (auto-rotates refresh tokens back to 1Password)
- `lib/gmail_search.py` — Gmail query builder + amount extraction
- `lib/purchases.py` — Purchase JSON body builder
- `lib/invoices.py` — Invoice JSON body builder
- `lib/fx.py` — currency conversion
- `lib/log.py` — JSONL audit logger
- `lib/qb_tags.py` — append "Billed as expenses to <customer>" to PrivateNote + manual-tag list
- `lib/qb_attachments.py` — multipart upload of PDFs to QB `/v3/.../upload`, linked to any entity (Purchase or Invoice)
- `lib/receipt_match.py` — derive per-Purchase Gmail queries from QB Purchase memos
- `lib/forwarded_receipts.py` — match Gmail forwards to QB receipts inboxes against Purchases by customer+direction tokens
- `lib/pdf_render.py` — primary: weasyprint HTML→PDF with images; fallback: fpdf2 text-only + image_to_pdf for phone photos
- `lib/gmail_fetch.py` — Gmail thread + attachment downloader, with HTML→text fallback
- `lib/google_photos.py` — Google Photos OAuth + search + download (subject to March-2025 partner-only restriction)
- `lib/sense_check.py` — deterministic date+vendor check + local-LLM (Ollama, `llama3.2:1b`) plausibility check; catches mis-tagged Purchases before invoice
- `lib/vision_extract.py` — receipt OCR via Claude Haiku 4.5 (advisory, never auto-writes)
- `lib/llm_costs.py` — per-call cost log → `~/Library/Logs/SAI/llm_costs.jsonl`
- `lib/fx_live.py` — historical FX rate via Frankfurter.app (ECB-sourced), on-disk cache
- `docs/workflow.md` — end-to-end workflow doc + SAI architecture compliance checklist + known gaps
- `canaries.jsonl`, `edge_cases.jsonl`, `workflow_regression.jsonl` — eval datasets

## Auth preconditions

1. **1Password CLI** (`op`) installed and signed in:
   ```bash
   brew install --cask 1password-cli
   op signin
   ```

2. **QuickBooks Online OAuth** completed once via the overlay's setup notes. After OAuth, the long-lived refresh token must be stored in a 1Password item that the overlay's `config/1password-refs.yaml` points to. The base skill **never** reads secrets from local files.

3. **Google OAuth** (Calendar + Gmail) — uses the SAI-wide credentials at `~/.SAI/credentials.json`, shared with other SAI skills.

## Operator overlay required

A minimal overlay folder must provide:

```
<overlay>/
├── config/
│   ├── identity.yaml          # operator-specific QB IDs (customer, vendor, account, item)
│   └── 1password-refs.yaml    # 1Password item names (no secrets in this file)
├── bookkeeping-rules.md       # persistent accounting rules captured across runs
└── trip_runs/<slug>/          # per-trip artifacts (created at runtime)
```

See the existing overlay at `~/Lutz_Dev/SAI/skills/receipt-collector/` for a worked example.

## Why two layers?

- **Base skill** = the *capability*. Stays generic so the same code works for anyone.
- **Overlay** = the *configuration*. Account IDs, vendor mappings, customer mappings, FX preferences, 1Password item names. Never shared, never checked into the base repo.

Secrets never live in either layer — they live in 1Password, referenced by name from the overlay.

## Idempotency

Every QB write carries a marker in `PrivateNote`:
- Purchases: `[sai-receipts:<trip_slug>] <line_name>`
- Invoices:  `[sai-invoice:<trip_slug>]`

Re-runs find the marker and skip rather than duplicate.

## Eval

- `canaries.jsonl` — 3 must-pass tests, including a check that **no secrets ever appear in overlay YAML**.
- `edge_cases.jsonl` — image-only receipts (placeholder flow), mixed-currency invoices, ticket-credit warnings, codeshare flights, personal-detour exclusions.
- `workflow_regression.jsonl` — end-to-end golden runs.

## Cleanup mode (deferred)

After each customer Invoice is sent, the operator may have left bookkeeping rules behind (e.g., "food while traveling is not reimbursable but is a travel-cost expense"). The `cleanup-pass` subcommand walks recent Purchases and applies those rules, surfacing ambiguities for operator review. Currently a stub — see `docs/cleanup-pass.md` for the roadmap (TBD).
