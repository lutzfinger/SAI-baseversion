# cost-compiler — implementation plan

**Written:** 2026-05-20
**Author:** design conversation between Claude (executor + design partner)
and Lutz (operator).
**Inputs:**
- SAI `PRINCIPLES.md` (1437 lines, public, durable rules)
- This skill's current `docs/STATUS.md` (2026-05-20 snapshot)
- This skill's current `docs/workflow.md` (canonical 10-step doc)
- Lutz's restated 10-step workflow (2026-05-20 session)

**Output:** phased gap-closure plan to ship the full 10-step
workflow with SAI compliance + reusable atomic steps.

**Companion docs:**
- `docs/STATUS.md` — where we ARE (snapshot at last compact)
- `docs/workflow.md` — the canonical 10-step description
- `docs/PLAN.md` (this file) — where we GO next

---

## Section 1 — SAI principles the plan is grounded in

The seven non-negotiables (`PRINCIPLES.md` §TL;DR):

1. **Eval is the purpose.** Every change gated by canaries +
   edge_cases + workflow_regression (#16a, #16d, #33).
2. **Reality is ground truth.** Model agreement doesn't count;
   only observed reality (operator action, explicit reply,
   co-work decision) flips ground truth (#11).
3. **Local-first execution.** Runs on operator's Mac. Cloud is a
   tool, not the system of record (#1, #12 cascade upward-only).
4. **Public ships mechanism; private ships values.**
   `SAI-baseversion/skills/<skill>/` = capabilities;
   `SAI/skills/<skill>/` = operator values (#17).
5. **Policy before side effects.** Every external write goes
   through the policy gate (#2, #9 two-phase commit).
6. **Fail closed.** Missing auth, ambiguous input, hash mismatch
   → refuse, never guess (#6, #6a).
7. **Drop, don't delete.** Skipped records stay in the audit log
   with reason (#27).

The plan inherits all seven and the more specific principles
that follow (#13 Pluggable Provider; #15 sample-rate
experimentation; #24b LLM choice configurable; #33 Skill plug-in
protocol; #33a Skills compose; #33b Co-Work designs, Claude
Code executes).

---

## Section 2 — Architectural-choice acknowledgement (Lutz's 5)

These were the explicit asks in the design conversation. Each is
restated, paired with the principle that backs it, and the
implementation status.

### 2.1 Local systems first

| Surface | Implementation | Status |
|---|---|---|
| Rules-tier deterministic logic | every cascade step in `skill.yaml` declares `kind: rules` first | ✅ |
| Local-LLM sense-check (Step 10) | Ollama `llama3.2:1b`, sub-second, $0 | ✅ |
| Local-LLM vision (Step 9 fallback) | Llava-style via Ollama | ⚠ Phase D |

Backed by principle #1 (local-first execution) + #12 (cascade
upward-only) + #24b (LLM choice configurable, no hardcoded model
names).

### 2.2 LLM as fallback, never default

| Surface | Implementation | Status |
|---|---|---|
| Cascade order | rules → local_llm → cloud_llm → human | ✅ in `skill.yaml` |
| Sense-check tier ordering | deterministic first, LLM only on MAYBE | ✅ in `lib/sense_check.py` |
| Vision-OCR (opt-in only) | only `extract-receipt-amounts` subcommand calls Haiku; nothing auto-invokes | ✅ |

Backed by #12 (cascade with early-stop) + #16f (agent execution
planes — guardrails-as-tools).

### 2.3 Eval-dataset first

| Dataset | Cases | Mode | Status |
|---|---|---|---|
| `canaries.jsonl` | 3 | hard_fail | ✅ |
| `edge_cases.jsonl` | 9 (cap=50) | soft_fail | ✅ |
| `workflow_regression.jsonl` | 4 | hard_fail | ✅ |
| `true_north.jsonl` (optional) | — | — | ⚠ Phase E.3 |

Backed by #10 (eval-centric architecture) + #16a (every eval
surface is an EvalDataset) + #16d (every workflow same shape) +
#16h (true-north dataset) + #33 (hard contract: missing eval
dataset = skill refuses to register).

### 2.4 Cost control

| Surface | Implementation | Status |
|---|---|---|
| Per-call cost log | `lib/llm_costs.py` → `~/Library/Logs/SAI/llm_costs.jsonl` | ✅ |
| Daily rollup | `lib.llm_costs.today_usd_total(skill)` | ✅ |
| Per-tier `cost_cap_per_call_usd` declared in `skill.yaml` | declared on every LLM tier | ✅ |
| Daily $ cap with hard stop | `LLM_DAILY_USD_CAP` in overlay | ⚠ Phase A.4 |
| FX-rate cache | `~/Library/Caches/SAI/fx_rates.json` | ✅ |

Backed by #15 (sample-rate experimentation), #16h (true-north
cost discipline `SAI_TRUE_NORTH_MAX_COST_USD=2.00`), #28 (hard
ceilings, not queues), #33 (cost_cap_per_invocation_usd in
manifest).

### 2.5 Security

| Surface | Implementation | Status |
|---|---|---|
| Secrets in 1Password only | every `op://` reference | ✅ |
| Service-account-only `op` access | `OP_SERVICE_ACCOUNT_TOKEN` auto-loaded from macOS Keychain via `~/.zshenv` | ✅ |
| No biometric prompts mid-run | `OP_BIOMETRIC_UNLOCK_ENABLED=false` | ✅ |
| Idempotency markers on every QB write | `[sai-receipts:<trip>]` / `[sai-invoice:<trip>]` in PrivateNote | ✅ |
| Two-phase commit on operator-approval writes | `outputs[].requires_approval: true` for Purchase + Invoice | ✅ |
| Boundary linter clean | `boundary_check_private_terms.txt` | ✅ (re-verified Phase A.5) |
| Hash-verified prompt loading (#23, #24c) | not yet — sense-check and vision prompts are inline strings | ⚠ Phase E.2 |

Backed by #2 (policy before side effects), #5 (least-privileged
connectors), #6 (fail closed), #6a (schema enforcement), #7
(secrets in 1P only), #7a (service-account-only), #9 (approval
as durable state), #23 (hash-verified loading), #24c (prompts
content-addressed).

---

## Section 3 — Naming: receipt-collector → cost-compiler

**Recommendation:** rename the skill from `receipt-collector` to
`cost-compiler` in Phase A.

**Reasoning:**

1. "Receipt collector" understates what the skill does. It
   collects receipts AND pulls credit-card lines AND scans
   Gmail forwards AND searches Google Photos AND extracts
   calendar pre-bookings AND builds the customer invoice AND
   tags + memos + attaches the bank-tx side. The unifying verb
   is **"compile every billable cost for a customer trip."**
2. The user explicitly used "COST COMPILER" as the new framing
   in this session.
3. The directory rename is a one-time cost; downstream skills
   and the runner CLI use the slug, so the change is localized.

**Migration plan:**

- `git mv skills/receipt-collector skills/cost-compiler` in both
  repos.
- Update `workflow_id: cost-compiler` in `skill.yaml`.
- Update `audit_log_path: ~/Library/Logs/SAI/cost-compiler.jsonl`.
- Keep the old log file readable (don't move it; just stop
  writing to it; new runs go to the new file).
- Update `bookkeeping_rules_path`, `trip_runs_root`, and any
  other path references in overlay's `identity.yaml`.
- Search-and-replace `receipt-collector` → `cost-compiler` in
  docs (README, STATUS, workflow, PRD).
- Re-run boundary linter.

**Out of scope for the rename:**
- Don't change the eval-dataset shapes.
- Don't change the runner subcommand names (`scan-cards`,
  `match-receipts-to-purchases`, etc.) — they still describe
  what they do. The skill-level name change is the only rename.

---

## Section 4 — Current state vs the 10-step target

Snapshot from `docs/STATUS.md` (2026-05-20). Each step lists
its primary subcommand and the remaining gap.

| # | Concern | Subcommand | Status | Remaining gap |
|---|---|---|---|---|
| 0 | Initiation (Slack / email / CLI) | `runner.py` argparse | ⚠ CLI only | Slack + email triggers |
| 1a | QB credit-card scan | `scan-cards` | ✅ | — |
| 1b | Gmail-forwards-to-QB-inbox | `attach-onsite-photos` | ✅ | — |
| 1c | Google Photos search | `scan-gphotos` | ⚠ may 403 | Google partner-only restriction; documented; manual operator action |
| 1d | Calendar pre-booked flights/hotels | `infer_trip_window` (window only) | ⚠ partial | Dedicated `extract-pre-bookings` subcommand |
| 2 | REVIEW + approval gate | `present_review` + `final-review.md` | ⚠ partial | Auto-post to surface + wait-for-reply loop |
| 3 | Create PDFs (Downloads) | `match-receipts-to-purchases` + `attach-onsite-photos` | ✅ | — |
| 4 | Live FX on date-of-purchase + log | `fx_live.py` library | ⚠ not wired | Integrate into `create-invoice`; log per line |
| 5 | INVOICE with attached PDFs | `create-invoice` | ✅ | — |
| 6 | Find + tag + memo + attach bank tx | `tag-purchases` + attach steps | ⚠ partial | Generic bank-tx search by amount/date (today only finds Purchases WE created); Tag column is manual UI (no API) |
| 7 | Flag missing bank tx | — | ❌ not started | `reconcile-billables` subcommand |
| 8 | Log every event | `lib/log.py` | ✅ | FX-rate writes missing one `log_event` call (Phase A.2) |
| 9 | LLM cost reporting + local-LLM-first | `lib/llm_costs.py` | ✅ for paid; ⚠ for local fallback | Llava local pass for vision; daily $ cap (Phase A.4 + D.1) |
| 10 | Sense check (final gate) | `sense-check` | ✅ | — |

---

## Section 5 — SAI compliance audit (re-verified 2026-05-20)

Carried forward from STATUS.md §"SAI architecture compliance —
verified clean." No new leaks found on this design pass.

| Audit category | Base (`SAI-baseversion`) | Overlay (`SAI`) |
|---|---|---|
| Credit-card numbers + brand names | ✅ none | `payment_accounts.*.last4` |
| Customer / vendor / driver IDs | ✅ none | `vendors.*.id`, `default_customer.id` |
| QB realm / account / item IDs | ✅ none | `expense_accounts.*.id`, `invoice_items.*.id` |
| Email addresses + 1P item names | ✅ none | `secrets.*.op_item`, `qb_receipts_inboxes` |
| Airline / hotel / rideshare brand strings | ✅ generic regex only | `sense_check.airline_hints`, `receipt_match.*` maps |
| Gmail thread IDs / Attachable IDs | ✅ none (eval data synthetic) | per-trip artifacts in `trip_runs/<slug>/` |

**Will this stay clean as Phase A-F land?** The plan is designed
to preserve the split:

- Slack trigger code → base ships the listener wiring; overlay
  carries the channel/workspace ID + token reference.
- Email trigger code → base ships the IMAP/Gmail-poll wrapper;
  overlay carries the SAI@ address + label name.
- Reconciliation logic → base ships the search-by-amount-date
  rules; overlay supplies tolerance defaults.
- Calendar pre-booking extraction → base ships the heuristic
  ("event title contains airline/hotel keyword AND date is
  weeks before trip start AND attendee match"); overlay
  supplies keywords.
- Daily $ cap → base reads `policy.daily_llm_cap_usd` from
  overlay; no number hardcoded.

The boundary linter (per #24) re-runs at every commit; any
backsliding fails the pre-commit hook.

---

## Section 6 — Implementation phases

Each phase is a self-contained shipping unit (per #26 big
changes ship as a sequence). Tests stay green between phases.
Boundary linter stays clean between phases.

### Phase A — Quick wins (target: 1-2 hours total)

| # | Task | Files touched | Time |
|---|---|---|---|
| A.1 | Rename `receipt-collector` → `cost-compiler` | `git mv` + sed-style updates to `workflow_id`, log paths, README/STATUS/workflow titles, overlay paths | 30 min |
| A.2 | Wire `fx_live` into `create-invoice`; write rate per line to audit log | `lib/invoices.py`, `runner.cmd_create_invoice`, `lib/fx_live.py` (add `log_event`) | 30 min |
| A.3 | Add `--currency EUR\|USD` override on `create-invoice` + auto-parse from trigger text | `runner.py` argparse, `lib/parse_trigger.py` (new) | 15 min |
| A.4 | Daily LLM-cost cap with hard stop | `lib/llm_costs.py` (add `enforce_daily_cap`), overlay `identity.yaml` (`policy.daily_llm_cap_usd`) | 30 min |
| A.5 | Re-run boundary linter, refresh STATUS.md | n/a | 5 min |

Outcome: every line of the customer invoice carries its
purchase-date FX rate in the audit log; operator can throttle
spend; rename complete.

### Phase B — Workflow completeness (target: ~1 day)

| # | Task | Files touched | Time |
|---|---|---|---|
| B.1 | `reconcile-billables` subcommand | `lib/reconcile.py` (new), `runner.cmd_reconcile` | 2 hr |
| B.2 | Approval gate primitive — `await-approval` shape (channel-aware stub) | `lib/approval.py` (new), runner subcommand `await-approval --trip --surface` | 2 hr |
| B.3 | Calendar pre-booking extraction subcommand | `lib/calendar_prebooking.py` (new), `runner.cmd_extract_prebookings` | 2 hr |
| B.4 | Cleanup-pass rule application | `lib/cleanup.py` (today a stub), `runner.cmd_cleanup_pass` | 2 hr |
| B.5 | Eval datasets: add cases for each new subcommand | `canaries.jsonl`, `edge_cases.jsonl`, `workflow_regression.jsonl` | 1 hr |

Outcome: steps 6 + 7 reach ✅; calendar pre-booking gap closed;
cleanup-pass actually applies rules.

### Phase C — Surface triggers (target: ~1 day)

This phase is the biggest UX win. Currently the operator runs
CLI subcommands; after Phase C they can say "@SAI file my INSEAD
May receipts" in Slack and the bot drives the pipeline,
replying inline.

| # | Task | Files touched | Time |
|---|---|---|---|
| C.1 | `parse_trigger` module — Slack/email/CLI → unified `TripRequest` | `lib/parse_trigger.py` (extend from Phase A.3) | 2 hr |
| C.2 | Slack listener + orchestrator | `lib/slack_runner.py` (new), uses LangChain/Slack-bolt per #25 standard libs | 3 hr |
| C.3 | Email listener (Gmail poll-and-respond) | `lib/email_runner.py` (new) | 2 hr |
| C.4 | Reply-on-same-surface invariant test | `workflow_regression.jsonl` new case | 30 min |

Per principle #16i — register `cost-compiler-triggers` channel
in `channel_allowed_discussion.yaml`. Per #16e — refuse
unrecognized intents with a friendly capability list.

Outcome: triggers work from all three surfaces with surface
continuity.

### Phase D — Local-LLM-first vision (target: ~1 hour)

| # | Task | Files touched | Time |
|---|---|---|---|
| D.1 | Llava local pass first, escalate to Haiku on low confidence | `lib/vision_extract.py` (add `local_first=True` branch using Ollama `llava:7b`) | 1 hr |
| D.2 | Eval: edge_case for "low-confidence local → Haiku escalation" | `edge_cases.jsonl` | 20 min |

Outcome: most photo-receipts processed at $0; Haiku only fires
when Llava abstains. Closes the local-LLM-first gap from
section 2.1.

### Phase E — Eval expansion + observability (target: half day)

| # | Task | Files touched | Time |
|---|---|---|---|
| E.1 | Verify each of Steps 0-10 has at least one canary or workflow_regression case | `canaries.jsonl`, `workflow_regression.jsonl` | 1 hr |
| E.2 | Migrate sense-check + vision prompts to hash-locked files (per #24c) | `prompts/cost-compiler/sense_check.md`, `prompts/cost-compiler/vision_extract.md`, `prompts/prompt-locks.yaml` | 1 hr |
| E.3 | Opt in to `true_north` dataset (per #16h) | `eval/cost_compiler_true_north.jsonl`, `skill.yaml` (`eval.datasets[].kind: true_north`) | 30 min |
| E.4 | LangSmith trace wiring (already declared in `observability.langsmith_project: SAI`) | verify it's actually emitting | 30 min |

Outcome: hash-verified prompts; full eval coverage; passive
observability.

### Phase F — Polish

| # | Task | Files touched | Time |
|---|---|---|---|
| F.1 | Google Photos OAuth operator-onboarding doc | `docs/onboarding-photos.md` | 30 min |
| F.2 | Slack triggers: `/sai-checkin` integration for two-phase commit (#9) | `lib/slack_runner.py` | 1 hr |
| F.3 | Graduation experiment shadow (per #15): try `llama3.2:1b` on sense-check at 100% (already there) — instead, try graduating vision OCR from Haiku to Llava at 20% | `skill.yaml` (`graduation_experiment` block), `lib/vision_extract.py` | 2 hr |

---

## Section 7 — Decisions (resolved 2026-05-20)

| # | Decision | Operator answer |
|---|---|---|
| 7.1 | Approve rename `receipt-collector` → `cost-compiler`? | **No — keep `receipt-collector` slug.** "Cost compiler" remains the colloquial description in workflow.md; directory/`workflow_id` unchanged. Phase A.1 is dropped. |
| 7.2 | Daily LLM-cost cap (`policy.daily_llm_cap_usd`) | **$5.00/day.** Generous headroom (~150× current daily spend) for vision-heavy trips. |
| 7.3 | Slack channel name for cost-compiler triggers | Defer to Phase C. Overlay-supplied; not blocking phases A/B. |
| 7.4 | SAI@ email address for email triggers | Defer to Phase C. Operator-supplied; not blocking phases A/B. |
| 7.5 | Phase ordering | **A → B → C → D as listed.** Operator instruction: "Do not ask for feedback but deliver. Test it yourself. Come back when finished." |

**Execution mode:** autonomous through phases A → B → C → D. All
non-obvious choices captured in `docs/DECISIONS.md`. Self-test
gates the phase boundary; the operator only sees the final
summary on completion.

---

## Section 8 — Out of scope for this plan

These were considered and intentionally excluded:

1. **New framework primitives.** Per principle #33a (Skills
   compose). A RAG retriever, a new Provider class, a new tier
   kind — all would be framework work, not skill work. None
   are needed for the 10-step workflow.
2. **Multi-operator support.** Per `PRINCIPLES.md` "What this
   system is not" — single operator. Stays that way.
3. **Auto-applying refinements.** Per #20 (reflection may
   suggest, never auto-apply). Cleanup-pass surfaces
   suggestions; only the operator applies them.
4. **Vendor-specific API hardcoding.** Per #24a + #24b. Stays
   LangChain-portable + registry-driven.
5. **A migration to a graph database or external store.** State
   stays local (SQLite + JSONL + per-trip folders) per #1, #8.

---

## Section 9 — Resume notes for next session

When you next open this skill to start work on the plan:

1. **Read `docs/STATUS.md` first** — the as-of-2026-05-20
   snapshot.
2. **Read this `docs/PLAN.md`** for the phased gap-closure
   path.
3. **Confirm Section 7 decisions** with the operator before
   touching A.1 (the rename ripples through ~40 files).
4. **Phase A first.** Quick wins; ship them before the bigger
   chunks.
5. **Before each phase ships:**
   - Run `python -m pytest tests/` for the skill (canaries +
     edge_cases + workflow_regression)
   - Run boundary linter (`scripts/check_boundary.py`)
   - Update STATUS.md with the new ✅ statuses
6. **Never rename `receipt-collector` in code without first
   running the boundary linter on the rename diff.** The slug
   appears in idempotency markers (`[sai-receipts:<trip>]`),
   audit log paths, and overlay path references. Catch any
   miss.
7. **Per principle #33b** — Claude Code executes Co-Work
   design. This plan IS the Co-Work design output. Stick to
   it. If during execution the design feels wrong, stop and
   bring the change back to a design conversation; don't
   redesign mid-execution.
