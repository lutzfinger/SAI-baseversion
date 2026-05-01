# SAI core principles

Read this first. These are the rules every change in SAI is judged against —
the architectural and operational defaults that shouldn't have to be argued
in each new session.

If a proposed change violates a principle, say so explicitly and propose a
deliberate exception with reasoning; don't silently break it.

---

## TL;DR (the seven non-negotiables)

1. **Eval is the purpose, not a side-effect.** The point of SAI is to grow
   a high-quality eval dataset. Cheaper tiers graduate when their P/R clears
   the threshold. Everything else serves this loop.
2. **Reality is the only ground truth.** Tier predictions — even unanimous
   ones — are transient. `is_ground_truth=True` flips ONLY on observed
   reality (Gmail re-tag, Slack-ask reply, calendar event, co-work approval).
3. **Public ships mechanism. Private ships values.** Schemas, contracts,
   factories, runners → public. Your taxonomy, prompts, OAuth tokens,
   Slack channel names, API keys → private.
4. **Cheapest tier first; cascade early-stops.** Rules → Classifier →
   Local LLM → Cloud LLM → Human. Most requests resolve in one tier; the
   most expensive runs only when the cheaper one abstains.
5. **Secrets never live in either repo.** `.env` files contain only
   `op://` (1Password CLI) or `keychain://` (macOS Keychain) references.
   Real values are pulled at runtime.
6. **Runtime state lives outside both repos.** `~/Library/{Logs,
   Application Support}/SAI/`, `~/.config/sai/tokens/`. Working trees stay
   code-and-config-only.
7. **Drop, don't delete.** Skipped records, expired asks, declined
   refinements — they stay in the audit log with a reason. Nothing
   vanishes silently.

---

## Why we exist

SAI is a **stack of intelligence** for routine personal-knowledge tasks
(email, meetings, travel, contacts). The stack is ordered cheapest →
most expensive:

1. **Rules** — deterministic, free, microseconds.
2. **Classifier** — small ML model, milliseconds, ~free.
3. **Local LLM** — Ollama / llama.cpp, runs on your hardware.
4. **Cloud LLM** — OpenAI / Anthropic / Gemini, dollars per million tokens.
5. **Human** — Slack ask, you decide, asynchronous.

**At runtime** we cascade upward only when needed: try rules first; if
rules abstain, try the next tier; stop at the active ceiling. The expensive
tier runs only when the cheap tier said "I don't know."

**At build time** we cascade downward: a new task starts at the cloud LLM
(because we don't know what works). As eval data accumulates, we graduate
local LLM → classifier → rules. Each graduation is human-approved based
on precision/recall against eval ground truth.

---

## Principles

### 1. Eval-centric architecture

- Every task input → one `EvalRecord` written to
  `eval/<task_id>/records.jsonl`. Append-only, JSONL, partitioned per task.
- `tier_predictions` field is **audit only**. The runner never trusts it
  as ground truth.
- `reality` is set only by:
  - `RealityReconciler` observing real-world signal (Gmail re-tag, calendar
    event, ...)
  - `AskReplyReconciler` parsing a Slack-ask reply
  - Explicit co-work approval (recorded as `RealitySource.COWORK`)
- `is_ground_truth=True` follows from any of the above. Once set, never
  unset. Used by `GraduationReviewer` for P/R, by training pipelines, by
  manual review.

### 2. Public / private split

- **Public** (`SAI-baseversion`):
  - Mechanisms: `Tier` Protocol, `Provider` Protocol, `ReplyParser` factory,
    `RealityReconciler` Protocol, `TieredTaskRunner`, `EvalRecordStore`,
    `SlackAskUI`, `option_matching_parser`, ...
  - Schemas: `EvalRecord`, `Preference`, `Ask`, `EmailMessage`,
    `EmailClassification` shape (without specific bucket values).
  - Universal infrastructure: cost table, hash-verified loader, overlay
    merge tool, boundary linter.
  - Worked examples: one `registry/tasks/email_classification.yaml` etc.
  - Documentation: `MIGRATION-BACKLOG.md`, `PHASE-3F-CUTOVER.md`, this file.

- **Private** (`SAI`):
  - Values: your L1 taxonomy (15 buckets), your L2 intents, your prompt
    content, your keyword baselines.
  - Identity: OAuth tokens (or `keychain://` references to them), Slack
    channel names, your watchlist, your team roster.
  - Per-task TaskFactories: `app/tasks/<task_id>.py` instantiate live
    tiers by passing private values into public factories.
  - Cron-style entrypoints (the launchd-invoked `scripts/run_*.sh`).
  - Eval data, preferences, audit logs.

- The **overlay merge** (`sai-overlay merge`) writes both into
  `~/.sai-runtime/` with private winning on path conflicts. Hash manifest
  generated; verified at startup.

- **Test for the split**: would you ship this file in an open-source
  starter to a stranger? If yes → public. If no → private. If you're not
  sure, it's private.

### 3. Cascade with early-stop, never parallel

- A `Task` lists tiers cheapest → most expensive. `active_tier_id` is the
  ceiling for normal operation.
- The runner walks tiers in order. The first tier whose prediction is
  non-abstaining AND clears its `confidence_threshold` resolves the request;
  cascade halts.
- `escalation_policy` determines what happens when the active tier abstains:
  `ASK_HUMAN` (most common), `USE_ACTIVE` (apply low-confidence anyway),
  `DROP` (raise).
- **Never** run all tiers on every input "to compare." Cost is real.
  Comparison happens via deliberate, time-bounded `graduation_experiment`
  blocks at sample rates of 10–20%.

### 4. Sequential cascade, observable cost

- Each tier returns a `Prediction` with `cost_usd`, `latency_ms`, `tier_id`,
  `abstained`, `confidence`, `output`.
- Cost flows up from the `Provider` via the cost table (`cost_table.yaml`,
  USD per million tokens, per provider+model).
- The `EvalRecord.escalation_chain` lists the tier_ids that actually ran.
  Length 1 = good; length N = the tier-improvement backlog.
- Optimization metric: **fewer escalations over time**. Every escalation
  is a signal that the cheaper tier could be improved.

### 5. Pluggable Provider abstraction

- `Provider` is the single Protocol that wraps a vendor SDK call. One
  `predict(LLMRequest) -> LLMResponse`.
- Switching from OpenAI to Anthropic, or from gpt-4o to claude-sonnet-4-5,
  is a YAML edit + a Provider class swap. **Never a tier rewrite.**
- Public ships four: `OpenAIResponsesProvider`, `AnthropicMessagesProvider`,
  `GeminiProvider`, `OllamaProvider`. Add more by following the same shape.
- Cost is the Provider's responsibility — it consults `cost_table.yaml`
  and emits `cost_usd` in the response.

### 6. Reality-only ground truth

- Three legitimate sources of `ObservedReality`:
  1. **Direct user action** in the system being mediated (Gmail re-tag,
     calendar event, booking confirmation).
  2. **Explicit Slack-ask answer** (typed reply parsed by the task's
     `ReplyParser`).
  3. **Co-work session approval** (real-time human+SAI session that
     produces a recorded decision).
- A model agreeing with itself or another model is **never** ground truth.
  Even when every tier returns the same answer, `is_ground_truth=False`
  until reality confirms.
- Co-work produces `Preference(strength=PROPOSED, source=COWORK)`. It
  applies to runtime only after a Slack-ask approval flips it to
  `SOFT` or `HARD`.

### 7. Secrets never in either repo

- `.env` files: only references — `op://Personal/SAI/openai_api_key` or
  `keychain://sai/openai_api_key`. Never literal `sk-...`.
- The `app/shared/runtime_env.py` loader resolves these at runtime:
  - `op://...` → 1Password CLI: `op read 'op://...'`
  - `keychain://service/account` → `security find-generic-password ...`
- Boundary linter has a rule that flags raw `xoxb-`, `sk-`, OAuth client
  secrets, JWT-shaped strings. Tripping it should be impossible if the
  rule above is followed.
- Tokens for OAuth flows live at `~/.config/sai/tokens/` — outside the
  repo. The path comes from `Settings.tokens_dir`, never hard-coded.

### 8. Runtime state outside the repo

- `~/Library/Application Support/SAI/state/` — sqlite databases,
  langgraph checkpoints, background-service heartbeats.
- `~/Library/Logs/SAI/` — append-only logs (audit.jsonl, scheduled-job
  stdout/err).
- `~/.config/sai/tokens/` — OAuth token files.
- `REPO_ROOT/eval/` — eval datasets, preferences. Versioned IF private,
  not committed in public.
- The `overlay merge` tool skips `logs/` and `quarantine/` by default —
  any leftover from old layouts gets ignored, not carried into the runtime.

### 9. Observability is built-in, not bolt-on

- LangSmith tracing infrastructure ships in **public**: tier-level traces,
  Provider call logs, cost rollups. The framework is open-source-ready.
- LangSmith **API keys** stay private (via `op://` reference).
- Every cascade run writes one EvalRecord (audit). Every Slack ask is one
  Ask record (audit). Every reconciliation outcome counted.
- Boundary linter ensures public never accidentally exposes traces with
  real customer email content.

### 10. Hash-verified loading, fail-closed

- `sai-overlay merge` produces `.sai-overlay-manifest.json` — SHA-256 of
  every file in the merged tree.
- The runtime verifier (`app/runtime/verify.py`) re-hashes on startup. Any
  mismatch (tampering, accidental edit, missing file) fails the control
  plane before it can do anything.
- Three modes (`strict`, `warn`, `off`) — production runs on `strict`.

### 11. Boundary-linter enforced

- `scripts/boundary_check.py` runs on every public commit (pre-commit hook
  + GitHub Actions).
- Catches: real email addresses (non-`example.com`), personal names
  (`lutzfinger`, `Lutz_Dev`, etc), `/Users/...` paths (except
  `/Users/example/`), real Slack channels (except `#general`,
  `#example`, `#test-channel`), phone numbers, secret-scheme refs.
- Per-file allowlist at `boundary_check_allowlist.txt`. **Every entry
  must have a comment justifying the exemption.** No bare allowlist
  entries.

### 12. Test before action; smoke before cutover

- Every Tier impl, Provider, parser, reconciler has unit tests.
- Every architectural component has integration tests (`tests/integration/`)
  that exercise the narrative end-to-end with mocks.
- Before any cutover (Phase 3F-style flipping launchd to a new path), run
  side-by-side for ≥1 week. Compare new outputs to current production.
- Rollback is always one command + one config edit. If you can't rollback
  in <60s, you're not ready to flip.
- Backtest harnesses (e.g. `scripts/backtest_*`) let you replay real data
  through the new architecture without affecting production.

### 13. Drop, don't delete

- `EvalRecord.mark_skipped(reason="...")` stays in the audit log. Excluded
  from training but visible.
- Failed Slack asks (channel missing, etc) become `Prediction(abstained=True,
  metadata={"ask_failed": True, "error_type": ...})`. Cascade continues,
  trail visible.
- Expired asks (past their reply window) get marked EXPIRED, never auto-
  deleted. The user can review what they didn't get to.
- "Why didn't this train?" should always be answerable from the JSONL log.

### 14. Hard ceilings, not queues

- Daily Slack-ask budget: hard cap (default 5/day/task). When exceeded,
  the orchestrator skips with `reason="below_priority_threshold"` or
  `reason="budget_exhausted"` — **never** queues for tomorrow.
- The system never burns through pending asks at midnight. Tomorrow's
  budget gets fresh priorities; missed records get a fresh decision based
  on tomorrow's coverage state.

### 15. Co-work for extraction; approval for application

- A real-time human+SAI session ("co-work") may produce inferred
  preferences ("Lutz prefers exit row"). These land as
  `Preference(strength=PROPOSED, source=COWORK)`.
- **Runtime ignores PROPOSED preferences.** They apply only after explicit
  human approval flips them to SOFT or HARD via a Slack ask reply.
- The `PreferenceRefiner` watches for violations of active preferences and
  proposes refined versions. Same rule: refinements are always PROPOSED
  until approved.
- Never edit a preference silently. Every change is a new
  `PreferenceVersion` with an approval ask_id linking it to the human's
  consent.

### 16. Fault-tolerant cascade

- Slack down? `HumanTier` catches the exception, returns
  `Prediction(abstained=True, metadata={"ask_failed": True})`. Cascade
  continues per escalation policy.
- LLM API timeout? Provider raises `LLMProviderError`; tier catches it,
  abstains. Cascade falls through to the next tier.
- Local Ollama unreachable? Same.
- Channel missing? Same.
- A run NEVER crashes because a downstream service is unreachable. The
  EvalRecord captures what we attempted; the operator sees the failure
  in the next reconciliation cycle.

### 17. Confirmation + clarification for human asks

- When the human replies validly, the reconciler posts a ✅ confirmation
  reply in the same Slack thread. Don't make the human wonder "did it
  receive my answer?"
- When the human replies with something the parser can't recognize, the
  reconciler posts a ❓ clarification listing the valid options. The ask
  stays OPEN; the human's next reply gets another shot.
- Replies are always thread-scoped (`conversations.replies` API), never
  channel-broadcast. The bot's own confirmations are filtered out by
  `bot_user_id` (resolved once via `auth.test`).

### 18. Sample-rate experimentation

- `graduation_experiment` blocks let a candidate tier shadow-run on a
  sample (default 10–20%) of inputs for a bounded window (default 14
  days).
- Compare candidate predictions to ground truth (from `RealityReconciler`).
- After window closes, `GraduationReviewer` posts a Slack ask with the
  P/R numbers asking "promote?". Human approves; `active_tier_id` flips.
- Never run shadow on 100% of inputs. Cost matters. Statistical
  significance comes from window length, not sample size.

### 19. Per-task migrations, never big-bang

- Each task migration is its own session: read the private original,
  identify framework vs data, write `registry/tasks/<id>.yaml` (public
  example), write tier impls (public framework), write integration test
  (public), write private TaskFactory (private). Smoke. Cutover.
- Never migrate multiple tasks in one go. Each is a 2-session unit
  (build + smoke + cutover).
- The MIGRATION-BACKLOG tracks deferred tasks with their target tier
  shape and missing dependencies — the punch list to work down.

### 20. Documentation is part of the change

- Every Phase or significant migration produces a phase report
  (`PHASE-N-DONE.md`) describing what landed, what's deferred, and how
  to operate it.
- Cutover runbooks (`PHASE-3F-CUTOVER.md`) are explicit step-by-step
  with rollback commands.
- Migration backlog entries describe the target tier shape, missing
  deps, and the private-repo source location.
- Code comments answer "why?", not "what?". The "what" is the code.

---

## Operating defaults

| Concern | Default |
|---|---|
| Tier confidence threshold (rules) | 0.85 |
| Tier confidence threshold (classifier) | 0.85 |
| Tier confidence threshold (local LLM) | 0.70 |
| Tier confidence threshold (cloud LLM) | 0.60 |
| Reality observation window | 7 days (email), 14 days (booking-style) |
| Daily Slack-ask budget | 5/task/day |
| Graduation experiment sample rate | 0.20 (20%) |
| Graduation experiment window | 14 days |
| Cost table currency | USD per 1M tokens |
| Eval channel | `#sai-eval` (single channel by default) |

Override per-task in `registry/tasks/<id>.yaml` or per-environment via
the appropriate `Settings` field.

---

## Working with Claude on this project

When you (or I) start a new Claude session for SAI work:

1. **Read this file first.** It saves re-explaining the rules.
2. **Read `MIGRATION-BACKLOG.md`** to see the active task-migration queue
   and target tier shapes.
3. **Read `PHASE-3F-CUTOVER.md`** if anything is changing about the
   runtime layout.
4. **Pick a discrete task scope.** A single task migration, a single
   refactor, a single Phase sub-step. Not "redo the architecture."
5. **State the principle being applied** when designing a change. Pin it
   to a numbered rule above so the reasoning is checkable.
6. **Test before commit.** Every change keeps `make test` green and the
   boundary linter clean. CI doesn't block; you do.
7. **Document deferred work.** If you don't land it now, file it in
   MIGRATION-BACKLOG with target shape and missing deps.
8. **Drop, don't delete.** When a piece of the framework changes shape,
   the old version goes to history (git tag, `_archived/` directory,
   doc note), not `git rm`.

---

## Glossary (one-liners)

- **Tier** — one rung of the cascade (rules / classifier / local_llm /
  cloud_llm / human). Conforms to `app.runtime.ai_stack.tier.Tier`.
- **Provider** — vendor-specific LLM SDK wrapper. Conforms to
  `app.llm.provider.Provider`.
- **Task** — a workflow with its own cascade, eval dataset, and
  preferences. Defined by `registry/tasks/<id>.yaml` + a private
  TaskFactory.
- **EvalRecord** — one processed task input with its tier predictions,
  active decision, and (eventually) ground truth.
- **Reality** — observed real-world signal (Gmail tag change, Slack
  reply, etc) — the only source of `is_ground_truth=True`.
- **Cascade** — sequential walk through tiers with early-stop on
  confidence.
- **Cutover** — moving production from old runtime path to new
  (`~/.sai-runtime/`) via launchd plist edit.
- **Overlay** — public + private merge model. Private wins on path
  conflicts.

---

*This document is itself a deliverable. Changes to it require the same
discipline as changes to code: state the principle being added or
modified, propose the wording, get review, commit with a clear message.
Drift between principles and practice is the root cause of every "wait,
why did we do it like this?" question — keep it tight.*
