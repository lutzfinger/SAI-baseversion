# cost-compiler — decisions log (Phase A-D execution)

Append-only record of non-obvious choices made during the
autonomous execution of `docs/PLAN.md` Phases A → B → C → D.
Each entry follows: **what was decided, why, what was rejected.**

The principle behind keeping this log: per `PRINCIPLES.md` #20
(Reflection may suggest, never auto-apply), an autonomous executor
should leave an audit trail of every non-trivial choice so a
human reviewer can audit later.

---

## 2026-05-20 — Phase pre-flight

### D-001: Skill rename rejected; "cost compiler" stays colloquial
- **Decided:** keep directory name `receipt-collector`,
  `workflow_id: receipt-collector`, audit log path
  `~/Library/Logs/SAI/receipt-collector.jsonl`.
- **Why:** operator explicit answer 2026-05-20.
- **Rejected:** `git mv` rename in PLAN Phase A.1.
- **Side effect:** workflow.md retains "cost-compiler" as the
  framing at the top with a "(currently shipped under
  `skills/receipt-collector/`)" note.

### D-002: Daily LLM-cost cap = $5.00
- **Decided:** `policy.daily_llm_cap_usd: 5.00` in
  overlay `identity.yaml`.
- **Why:** operator explicit answer 2026-05-20. ~150×
  current daily spend ($0.034) — generous for vision-heavy
  trips.
- **Rejected:** $1.00, $2.00 alternatives.

### D-003: Phase ordering A → B → C → D, autonomous
- **Decided:** execute A, B, C, D in order; document
  decisions here; self-test; return only on completion.
- **Why:** operator explicit instruction 2026-05-20.
- **Implication:** no AskUserQuestion until phases land
  unless a hard blocker emerges (operator-only setup like
  Slack workspace tokens or Gmail SAI@ address).

---

## 2026-05-20 — Phase A execution

### D-004: FX cascade order = live (Frankfurter/ECB) → static fallback → raise
- **Decided:** `_resolve_fx` tries `fx_live.get_rate` first,
  catches any Exception, then looks up `fx_fallback_table`
  from the overlay. If both miss, raises ValueError.
- **Why:** per SAI #6 fail-closed — never silently guess a
  rate. Also satisfies #1 (local-first cache after first
  hit) since `fx_live` caches at
  `~/Library/Caches/SAI/fx_rates.json`.
- **Rejected:** silent fallback to `1.0`, last-known-rate,
  static-only (loses ECB precision).

### D-005: Line-spec backward compatibility
- **Decided:** legacy lines (no `source_currency`) build
  unchanged. FX only fires when `source_currency != invoice_currency`
  AND `txn_date` is set. Missing `txn_date` on a cross-currency
  line is a hard error (ValueError).
- **Why:** the operator's existing plan.json files are
  pre-converted in invoice currency; we don't want to break
  them. Cross-currency lines opt in by adding two fields.

### D-006: FX audit log row shape
- **Decided:** each FX lookup writes one `fx_lookup` event
  via `log_event` with keys: from_ccy, to_ccy, rate, source
  (live/static/identity), on_date, original_unit_rate,
  converted_unit_rate, purchase_id, line_description.
- **Why:** lets `~/Library/Logs/SAI/receipt-collector.jsonl`
  answer "which rate did I use on which line on which day"
  for any audit query.

### D-007: --currency CLI override precedence
- **Decided:** `--currency` (CLI) > `plan.invoice_currency`
  (file) > customer's QB currency > "USD".
- **Why:** CLI is the most-recent operator intent. The plan
  file is durable but older. Customer default is fine for
  new customers without explicit currency preference.

### D-008: Currency aliases in parse_trigger
- **Decided:** parser maps "euro", "euros", "€" → "EUR";
  "dollar", "dollars", "$" → "USD"; etc. Default = USD when
  no token matches.
- **Why:** the operator wrote: "If he does not say anything
  assume DOLLAR." (2026-05-20 spec.)

### D-009: Daily LLM cap = no-op when not configured
- **Decided:** `enforce_daily_cap` returns silently when
  overlay has no `policy.daily_llm_cap_usd` value.
- **Why:** the cap is operator-supplied; the base skill
  doesn't know what number is right. Tests can pass
  `cap_usd=` directly. Production overlay sets it to $5.00
  per D-002.
- **Rejected:** hardcoded base default — would be a leak per
  the public/private split (#17).

### D-010: BudgetExceeded raises BEFORE the API call
- **Decided:** `vision_extract.extract_receipt` calls
  `enforce_daily_cap` as the FIRST action. If it raises,
  no Anthropic SDK is even imported.
- **Why:** principle #28 (hard ceilings, not queues) +
  cost discipline. The estimate is conservative ($0.01 for
  a typical Haiku call that runs ~$0.003) so we never
  accidentally tip over.

## 2026-05-20 — Phase B execution

### D-011: Reconcile tolerance defaults
- **Decided:** amount tolerance = max($0.50 abs, 0.5% pct);
  date tolerance = ±2 days.
- **Why:** card-network rounding sometimes shifts cents
  $0.01-$0.05 (taxi tipping in particular). International
  transactions post 1-2 days late. CLI flags
  `--amount-tol-abs/pct` and `--date-tol-days` let the
  operator narrow when needed.

### D-012: Reconcile defaults to overlay-payment-accounts only
- **Decided:** `reconcile-billables` filters QB Purchases
  to the overlay's `payment_accounts.*.id` set. Pass
  `--include-all-cards` to scan everything.
- **Why:** if the operator runs personal cards in QB too,
  every dinner on their personal card would show up as
  "extras." Restricting to known business cards by default
  keeps signal-to-noise high.

### D-013: Approval surface stub = cli + file (Slack/email = Phase C)
- **Decided:** Phase B ships `cli` (stdin prompt) and `file`
  (sentinel file in `~/Library/Application Support/SAI/...`).
  Slack/email surfaces post sentinels in Phase C.
- **Why:** keeps the durable-state primitive testable today;
  Phase C only adds the message-poller, not new state.

### D-014: Approval reply parser is multi-strategy (#6a)
- **Decided:** match priority: exact full text → multi-word
  phrase substring → single-word in split. Anything else =
  "feedback" (logs to audit, keeps request OPEN).
- **Why:** "looks good to me" should approve. "wait a sec"
  should be feedback, not silent approval. Per #6a +
  #16g + #30.

### D-015: Pre-booking kind tie-break favors hotel
- **Decided:** if both airline and hotel regex hit on an
  event, classify as `hotel`.
- **Why:** "Hotel Ermitage booking confirmed" has "hotel"
  (specific) and "booking" (generic). Hotel is the more
  specific signal. Generic terms like "booking" /
  "reservation" stay OUT of both kind patterns.

### D-016: Cleanup-pass writes proposals, never applies (#20)
- **Decided:** `cleanup-pass` writes a markdown proposal doc
  under `~/Downloads/cleanup-proposals.md`. No QB writes.
  A future `apply-rule --rule <id> --confirm` will apply.
- **Why:** principle #20 — reflection may suggest, never
  auto-apply. Cleanup-pass is reflection.

### D-017: Rule parser splits on `## ` headers, not `## Rule`
- **Decided:** the markdown rule parser splits on every H2
  header but only keeps blocks starting with "Rule R<n>".
- **Why:** earlier version split only on `## Rule` which
  meant the LAST rule slurped the trailing "Cleanup pass
  (TBD)" narrative into its Triggers field, polluting the
  keyword extraction with "cleanup, pass, tbd".

### D-018: Slash-separated trigger terms split into alternatives
- **Decided:** "restaurant/cafe/bistro/bar" → 4 individual
  keywords (each a separate word in the trigger regex).
- **Why:** the operator writes terms naturally; the parser
  shouldn't require structured input.

## 2026-05-20 — Phase C execution

### D-019: Slack listener uses conversations.history poll, not socket-mode
- **Decided:** simple long-poll on `conversations.history`
  every 5s.
- **Why:** per principle #25 (standard libs before custom
  code), `slack-sdk`'s minimal `WebClient` is enough and
  doesn't require an open socket. Operators can upgrade to
  socket-mode later if they want sub-second latency.

### D-020: Slack scope = chat:write + channels:history + reactions:read
- **Decided:** minimum scope for trigger → status reply →
  reaction-based approval. Not `chat:write.public` (would
  let bot post anywhere).
- **Why:** principle #5 (least-privileged connectors).

### D-021: Email uses a SEPARATE OAuth token for gmail.send
- **Decided:** `~/.SAI/gmail_token.json` (read scope)
  remains for receipt fetching; `~/.SAI/gmail_send_token.json`
  is the send-scope token used ONLY by `email_runner`.
- **Why:** principle #5 again — don't grant send to the
  generic Gmail readers. Two tokens = two failure surfaces
  the operator can revoke independently.

### D-022: Email trigger gated on Gmail label, not subject
- **Decided:** operator must apply the `sai-trigger` label
  (configurable in overlay) to the email. The listener
  queries `label:sai-trigger is:unread from:<operator>`.
- **Why:** subject-based gating is easy to spoof from any
  sender; label-based requires the operator to have
  explicitly added the label, which is a much stronger
  intent signal.

### D-023: Listener subprocess-out to runner subcommands
- **Decided:** Slack and email runners spawn
  `python3 -m skills.receipt-collector.runner <step>` as a
  subprocess for each atomic step in the plan.
- **Why:** each atomic step ALREADY logs to JSONL audit and
  exit-codes its result. Reusing them via subprocess gives
  the surface runners the same audit shape for free. Future
  optimisation: in-process call if startup overhead matters.

## 2026-05-20 — Phase D execution

### D-024: Cascade abstains on local "low" confidence OR null total
- **Decided:** `extract_receipt_cascaded` escalates to
  cloud when local result has `confidence in ("low", None)`
  OR `total is None`. Otherwise stops at local (cost = $0).
- **Why:** "low confidence" includes the "Ollama not
  installed" and "llava not pulled" cases automatically —
  the local function returns those exact verdicts. So
  `--local-first` is safe to default ON; if the operator
  hasn't pulled llava, the cascade just transparently uses
  cloud.

### D-025: Local-tier hits still log a $0 cost row
- **Decided:** `cmd_extract_amounts` writes one
  `llm_costs.jsonl` row per call regardless of tier — paid
  rows track Haiku cost; local rows track tier="local"
  with `usd_cost=0`.
- **Why:** lets dashboards graph cascade hit rate (local %
  vs cloud %) without needing a separate counter. Per #4
  (append-only audit) the log IS the answer.

### D-026: --local-first defaults to ON
- **Decided:** the cascade is ON by default; `--no-local-first`
  forces cloud-only.
- **Why:** principle #1 local-first execution. The default
  honors the principle; operators only opt out for
  deterministic regression runs (the cheap-tier graduation
  experiment block, per #15).

## 2026-05-20 — Phase E.0 (trigger agent, replaces regex parser)

### D-027: Replace regex `parse_trigger.parse` as primary trigger interpreter
- **Decided:** new `lib/cost_compiler_agent.py` runs Claude
  Haiku with a guarded tool surface (mirroring the SAI
  `app/agents/sai_eval_agent.py` pattern). The agent inspects
  QB customers + Google Calendar + overlay metadata and
  proposes a structured plan. `parse_trigger.parse` is kept
  as the rules-tier FALLBACK (per #29 fault-tolerant cascade)
  when the LLM is unreachable.
- **Why:** operator explicit instruction 2026-05-20: "don't
  do a parser. See how we implement it in SLACK. You use HAIKU
  to evaluate what I want and propose a plan."
- **Rejected:** continuing to extend the regex parser with
  more date formats / customer aliases / currency aliases.
  Every new regex was another sharp edge.

### D-028: Direct Anthropic SDK, not LangChain
- **Decided:** the skill-local agent uses
  `anthropic.Anthropic().messages.create(tools=...)` directly,
  not LangChain's `create_agent`.
- **Why:** the slack-eval agent uses LangChain because it's
  part of the SAI v8 cascade framework with shared registry +
  prompt loader + observability. A skill-local agent is
  cleaner with the Anthropic SDK directly (the operator
  already has the SDK installed for vision_extract). The
  surface YAML keeps the contract auditable regardless of
  the runtime framework.
- **Promotion path:** if a second skill needs a
  trigger-to-plan agent, lift this into a SAI framework
  primitive at `app/agents/cost_compiler_agent.py` per
  principle #33a.

### D-029: Tool surface = read-only + ONE propose-only
- **Decided:** 4 read-only tools (`list_qb_customers`,
  `search_calendar_events`, `list_payment_accounts`,
  `list_expense_accounts`) + 1 propose-only tool
  (`propose_plan`). No mutate tools.
- **Why:** principle #16f — agent execution planes with
  guardrails-as-tools. The agent CANNOT mutate state
  directly; even the propose tool only writes a JSON
  proposal that the existing `await-approval` gate then
  approves. Two-phase commit preserved.

### D-030: `propose_plan` rejects customer_ids not surfaced earlier
- **Decided:** `propose_plan` validates that `customer_id`
  appears in `ctx.seen_customer_ids` — i.e., the agent
  ALREADY called `list_qb_customers` and saw that ID in this
  invocation.
- **Why:** principle #6a (schema enforcement at every
  boundary). Prevents the LLM from hallucinating customer
  IDs even if the model is confidently wrong. The verified
  test ("INSEAD" → 2 candidates in QB, agent asked rather
  than guessed) shows this works as designed.

### D-031: Agent honors daily LLM cap + writes per-call cost rows
- **Decided:** every iteration of the tool-use loop calls
  `llm_costs.enforce_daily_cap` BEFORE the API call and
  `llm_costs.log_call` AFTER. On cap-exceeded mid-loop, the
  agent falls back to the rules-tier `parse_trigger.parse`
  with a clear "⚠️ LLM agent unreachable" operator message.
- **Why:** principle #28 (hard ceilings, not queues) +
  #29 (fault-tolerant cascade). The cap is real; the
  fallback ensures the operator gets SOMETHING.

### D-032: Iteration cap = 6 (slack-eval uses 8)
- **Decided:** MAX_ITERATIONS=6 for cost-compiler trigger
  agent. Slack-eval has 8.
- **Why:** trigger interpretation is shallower than the
  Gmail thread-walk the slack-eval agent does. Verified:
  the operator's exact trigger landed in 3 iterations
  (`list_qb_customers` → `propose_plan` with one
  intermediate LLM turn). 6 iterations is generous
  headroom.

---

## 2026-05-20 evening — sai@ becomes a full Claude-via-email interface

### D-033: sai@ is multi-workflow; dispatch_agent replaces intent_router
- **Decided:** the operator's `sai@lutzfinger.com` inbox is
  not a dedicated channel for the cost-compiler. It's a
  shared SAI inbox carrying:
    - cost-compiler triggers
    - eval feedback for the sai-eval workflow
    - email-to-calendar forwards (operator's existing skill)
    - general questions the operator asks (research, jokes,
      opinions, code help)
    - workflow-suggestion descriptions
    - pure noise (boarding passes, vendor newsletters, etc.)
  `lib/dispatch_agent.py` classifies every incoming email
  into one of 5 verdicts: COST_COMPILER, EVAL_FEEDBACK,
  GENERAL_QUERY, WORKFLOW_SUGGESTION, IGNORE. Rules tier
  first (zero LLM cost on obvious cases), Haiku tier when
  rules abstain.
- **Why:** operator explicit request 2026-05-20 — "I would
  like to ask ANYTHING to this email... similar to SLACK."
- **Rejected:** keeping intent_router with 3 verdicts +
  forcing operator to use `sai+receipts@` for cost-compiler
  triggers. The shared-inbox UX matches how the operator
  actually uses email.

### D-034: New verdict GENERAL_QUERY → general_assistant.respond_to_query
- **Decided:** when classified as GENERAL_QUERY, the daemon
  invokes `lib/general_assistant.respond_to_query` which
  runs Claude (Haiku) with the Anthropic `web_search_20250305`
  server tool. The model decides per-question whether to
  search the web or just answer.
- **Why:** "if I ask 'research X' it should send back a
  research X answer as if I would have asked Claude
  directly" (operator 2026-05-20).
- **Cost shape:** plain chat (jokes/opinions) ~$0.001;
  research with 1-3 web searches ~$0.01-0.05. Honors daily
  cap.

### D-035: New verdict WORKFLOW_SUGGESTION → propose_workflow (NEVER creates)
- **Decided:** when the operator describes a workflow they
  wish existed, the bot replies with a structured PROPOSAL
  (acknowledgment + trigger + reads + produces + approvals +
  reusable steps + what would have to be built). The reply
  body ends with a hard-coded footer: "This is a PROPOSAL
  only. New workflows have to be built via Co-Work or
  Claude Code; the email channel can't create or modify
  SAI itself."
- **Why:** SAI principle #9 (channel-and-pattern-locked
  operator edits) — email is not a registered edit channel.
- **Rejected:** wiring email into a "build it" path. Even
  if the operator asks, email can't change SAI structure.
  Co-Work is the path.

### D-036: IGNORE verdict — silent file, no reply
- **Decided:** boarding passes, alumni invites, vendor
  newsletters, shipment notifications → marked read,
  audited to `dispatch_agent.jsonl`, NO reply sent.
- **Why:** the operator explicitly forwards these for
  filing, not for engagement. Replying to every forward
  would be the same spam pattern that caused the 8-email
  cascade earlier. The 5th verdict is the "silently file"
  outcome that preserves the no-spam invariant.

### D-037: EVAL_FEEDBACK now gets an acknowledgment reply
- **Decided:** when verdict=EVAL_FEEDBACK, the bot now
  sends a brief reply ("Got it — I logged your label
  correction. The eval workflow will pick this up...")
  AND logs to `eval_feedback_inbox.jsonl`.
- **Why:** operator explicit request — "If I ask 'wrong
  label' - it should answer that it did the workflow for
  wrong label" (2026-05-20). The previous silent-log
  behavior left the operator wondering if anything
  happened.

### D-038: Conversational email formatter (lib/email_format.py)
- **Decided:** all cost-compiler bot replies route through
  `lib/email_format.py` which renders:
    - `plan_staged()` — "Got it — I'll pull together your
      INSEAD trip receipts from May 5-18, billing in EUR..."
    - `collect_phase_done()` — friendly summary at top;
      technical details (slugs, paths, exit codes) collapsed
      into a `Details` section at the bottom.
    - `collect_phase_failed()` — surfaces the failure as a
      colleague would, with the parsed error summary instead
      of a raw traceback.
    - `invoice_done()` — "Done. Invoice X is in QuickBooks
      ($Y EUR). I did NOT send it — it's a draft for your
      review."
- **Why:** operator explicit feedback 2026-05-20 — "The
  actual text of feedback is non human readable. It's way
  too badly written. Hard to read."
- **Rejected:** running every reply through Claude for
  tone-polish. Adds cost + latency without much win;
  hand-written templates with conversational wording are
  enough.

### D-039: Cost-compiler agent prompt — re-propose allowed across operator messages
- **Decided:** `prompts/cost_compiler_agent.md` updated to
  say: "Do NOT call `propose_plan` more than once **within
  this same invocation**. (Each new operator message starts
  a fresh invocation — if the operator asks you to retry,
  you SHOULD call `propose_plan` again in the fresh turn.)"
  Plus an explicit "Do NOT lecture the operator about your
  role limitations when they ask you to retry."
- **Why:** the previous prompt caused Haiku to refuse
  re-staging the plan after a collect-phase failure, even
  when the operator explicitly asked. Multi-attempt flows
  got stuck.

### D-040: Collect-phase steps split blocking vs. advisory
- **Decided:** `scan-cards` + `search-receipts` are
  BLOCKING (failure → DROPPED). `attach-onsite-photos` +
  `extract-pre-bookings` are ADVISORY (failure → log +
  continue; surface to operator as "you may want to re-run
  these manually"). The plan still reaches AWAITING_APPROVAL
  even when advisory steps fail.
- **Why:** the macOS TCC bug (daemon subprocess can't write
  to ~/Downloads/) was hard-blocking the plan even though
  the photo PDFs are operator-review-only, not invoice-
  blocking. Tolerance on advisory steps lets the operator
  approve the invoice without the photos issue.

### D-041: Daemon subprocess output → ~/Library/Application Support/, not ~/Downloads/
- **Decided:** the daemon's collect-phase subprocess writes
  PDFs to
  `~/Library/Application Support/SAI/receipt-collector/downloads/`
  instead of `~/Downloads/`. The path is not TCC-protected
  so the launchd-spawned subprocess can write to it without
  permission errors. The operator copies / symlinks to
  `~/Downloads/` interactively if they want.
- **Why:** confirmed PermissionError "Operation not
  permitted" when the daemon's child process wrote to
  `~/Downloads/sai-photos-...`. The parent daemon CAN write
  there (verified via `bsexec` probe), but the permission
  doesn't inherit to subprocess.run children for some
  launchd configurations.
- **Better fix (future):** grant the daemon Full Disk
  Access in System Settings → Privacy & Security → Full
  Disk Access. That would let the subprocess inherit
  access too.
