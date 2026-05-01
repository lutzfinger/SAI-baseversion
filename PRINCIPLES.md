# SAI core principles

Read this first. These are the durable rules every change in SAI is judged
against — the architectural and operational defaults that don't get
relitigated each session.

If a proposed change violates a principle, name it and propose a deliberate
exception with reasoning. Don't silently drift.

This file holds **durable principles only**. Phase plans, current-cycle
migration sequencing, and "what's done so far" live in `MIGRATION-PRINCIPLES.md`
(dated, replaceable). Anything time-bound or task-specific belongs there.

---

## TL;DR — the seven non-negotiables

1. **Eval is the purpose.** The system exists to grow a high-quality eval
   dataset; cheaper tiers graduate when their P/R clears the threshold.
   Everything else serves this loop.
2. **Reality is the only ground truth.** Tier predictions — even unanimous
   ones — are transient. Ground truth flips ONLY on observed reality
   (real-world action, explicit human approval, co-work decision).
3. **Local-first execution.** SAI runs on the operator's Mac. Cloud
   services are tools, not the system of record.
4. **Public ships mechanism. Private ships values.** Schemas, factories,
   runners → public. Taxonomies, prompts, tokens, channel names, keys
   → private.
5. **Policy before side effects.** Every external action passes through
   the control plane gate before it happens. Workers don't decide their
   own permissions.
6. **Fail closed.** Missing auth, hash mismatch, unknown action, ambiguous
   input — all refuse. The default for ambiguity is to stop, not guess.
7. **Drop, don't delete.** Skipped records, expired asks, declined
   refinements stay in the audit log with a reason. Nothing vanishes
   silently.

---

## Why we exist

SAI is a **stack of intelligence** for routine personal-knowledge tasks.
The stack is ordered cheapest → most expensive:

1. **Rules** — deterministic, free, microseconds.
2. **Classifier** — small ML model, milliseconds, ~free.
3. **Local LLM** — Ollama / llama.cpp on the operator's hardware.
4. **Cloud LLM** — vendor-pluggable (OpenAI / Anthropic / Gemini / ...).
5. **Human** — Slack ask, asynchronous, decisive.

**Runtime cascades upward only when needed.** Try rules first; if they
abstain, try the next tier; stop at the active ceiling. The expensive tier
runs only when the cheap tier said "I don't know."

**Build time cascades downward.** A new task starts at cloud LLM (because
we don't know what works yet). As eval data accumulates, we graduate
local LLM → classifier → rules. Every graduation is human-approved,
gated on precision/recall against eval ground truth.

---

## Principles

### Privacy & trust

#### 1. Local-first execution

SAI runs on the operator's Mac. Local SQLite for state, JSONL for audit,
local artifact directories, local OAuth tokens. Cloud services are tools
the operator chooses, not the source of truth.

#### 2. Policy before side effects

Every external action — sending an email, writing a calendar event,
posting to Slack, calling an API that mutates state — passes through
the control plane policy gate before it happens. The gate consults the
workflow's policy file and returns allow / deny / approval-required.
Workers don't decide their own permissions.

#### 3. Approval as durable state

When a workflow needs human approval, that approval is a row in SQLite,
not a blocking prompt. Crashes, restarts, reboots leave approvals
intact and resumable. The approval queue survives the worker that
created it.

#### 4. Append-only audit

Every gate decision, connector call, approval transition, verification
failure, and reality observation writes a JSONL row. The audit log is
the answer to "what did the system do." Audit files don't get rewritten;
they get rotated and compacted, never edited.

#### 5. Least-privileged connectors

Per-workflow OAuth tokens with the narrowest scope each workflow needs.
`gmail.readonly` for classification, `gmail.modify` for tagging,
`gmail.send` only for workflows that genuinely send. No "super token"
shared across all Gmail uses. Same shape for every API.

#### 6. Fail closed

Missing auth fails. Hash mismatch fails. Unknown action fails. Reachable
service returning unexpected schema fails. The default for ambiguity is
to refuse with a clear error, never to guess.

#### 7. Secrets never live in either repo

`.env` files contain only references — `op://Personal/SAI/openai_api_key`
or `keychain://sai/openai_api_key`. Never literal `sk-...`, `xoxb-...`,
or any other vendor token. Real values are pulled at runtime via the
1Password CLI or macOS Keychain bridge.

#### 8. Runtime state outside the repo

`~/Library/Application Support/SAI/state/` for sqlite + checkpoints +
heartbeats. `~/Library/Logs/SAI/` for append-only logs.
`~/.config/sai/tokens/` for OAuth tokens. Working trees stay
code-and-config-only. The boundary linter prevents committing anything
state-shaped.

---

### Eval & cascade

#### 9. Eval-centric architecture

The system's purpose is to grow a high-quality eval dataset. Every task
input produces one append-only `EvalRecord` partitioned by `task_id`.
Tier predictions live in the record for audit but never count as ground
truth. Cheaper tiers graduate when their precision/recall against
ground-truth records clears the threshold and a human approves.

#### 10. Reality-only ground truth

Three legitimate sources of `ObservedReality`:

- **Direct user action** in the system being mediated (Gmail re-tag,
  calendar event, booking confirmation).
- **Explicit reply to a Slack ask** (typed answer, parsed by the task's
  ReplyParser).
- **Co-work session approval** (real-time human+SAI collaboration that
  produces a recorded decision).

Models agreeing with each other is never ground truth. Even a unanimous
cascade leaves `is_ground_truth=False` until reality confirms.

#### 11. Cascade with early-stop, never parallel

Tasks list tiers cheapest → most expensive. The runner walks them in
order; the first tier whose prediction is non-abstaining AND clears its
confidence threshold resolves the request. Cascade halts. Most requests
resolve in one tier. The expensive tier is the long tail, not the
default.

Never run all tiers on every input "just to compare." Comparison happens
via deliberate, time-bounded `graduation_experiment` blocks at sample
rates of 10–20%.

#### 12. Pluggable Provider abstraction

A `Provider` is the single Protocol that wraps a vendor SDK call:
`predict(LLMRequest) -> LLMResponse`. Switching from OpenAI to Anthropic,
or swapping models, is a YAML edit + a Provider class swap. Never a tier
rewrite.

Cost is the Provider's responsibility — it consults `cost_table.yaml`
(USD per million tokens, per provider+model) and emits `cost_usd` in
every response. Cost rolls up to per-task ROI dashboards automatically.

#### 13. Pluggable factories everywhere

The pattern repeats: when you need task-specific behavior on top of a
universal mechanism, ship the mechanism as a factory in public; private
wires values into it. Examples:

- `option_matching_parser(options=...)` → public factory; private wires
  its taxonomy.
- `RealityReconciler` Protocol → public; concrete reconcilers wire
  task-specific observation surfaces.
- `Tier` Protocol → public; concrete tiers wire prompts/rules/etc.

If you find yourself duplicating logic across two task implementations,
extract to a public factory. The duplicated piece IS the framework.

#### 14. Sample-rate experimentation

Comparing a candidate cheaper tier against the active tier doesn't run
on 100% of inputs — that defeats the cost goal. `graduation_experiment`
blocks let a candidate shadow-run on a sample (default 10–20%) for a
bounded window (default 14 days). Statistical significance comes from
window length, not sample size.

After the window closes, the GraduationReviewer compares candidate
predictions to ground truth and posts a Slack ask: "Promote? P/R numbers
attached." Human approves; `active_tier_id` flips.

#### 15. Co-work extracts, only approval applies

Real-time human+SAI sessions ("co-work") may produce inferred preferences.
These land as `Preference(strength=PROPOSED, source=COWORK)`. Runtime
ignores PROPOSED preferences. Application requires explicit human
approval flipping the strength to SOFT or HARD.

The PreferenceRefiner watches for violations of active preferences and
proposes refined versions. Same rule: refinements are PROPOSED until
approved. Preferences are never edited silently.

---

### Engineering discipline

#### 16. Public ships mechanism. Private ships values.

Mechanisms (validation, parsing, cascading, reconciling, persistence,
verification) are open-source-ready public framework. Values (the actual
taxonomy, the actual prompts, OAuth tokens, channel names) live in the
operator's private overlay.

Test for the split: would you ship this file in an open-source starter
to a stranger who's never seen the operator's data? If yes → public.
If no → private. If you're not sure, it's private.

#### 17. File-level override only in the public/private overlay

The overlay merge writes both repos into one runtime tree, with private
winning on path conflicts. **No deep YAML merging.** If private has
`workflows/x.yaml`, it replaces public's `workflows/x.yaml` entirely.
Per-key merging silently changes behavior; replacement is auditable.

#### 18. Smallest correct scope

Every workflow lists the exact tools it can call. Adding a tool requires
editing its policy file, which is a reviewable diff. Every connector
gets the narrowest API scope it needs. Don't grant blanket capabilities
"in case." Add specifically when needed.

#### 19. Reflection may suggest, never auto-apply

The system can propose prompt or policy improvements (and should). It
**cannot apply them**. Application requires a human-driven check-in
path with hash stamping and tests. Auto-applying changes from the same
agent that observes their effect is the trust failure that ends careers.

#### 20. No surface certifies its own deployment

The surface that drafts a change is never the surface that approves it
for production. Different roles, different tools, different sessions.
This rule is what makes the audit trail mean something — the writer
and the deployer are separated by intent.

#### 21. Naming hygiene

"SAI" is the framework. `sai-email` is a channel/workflow. `sai-run`
is the bridge skill. `sai-eval` is the human-feedback channel. Don't
collapse these. When you introduce a new namespace component, prefix it
clearly and document it in this section's glossary.

#### 22. Hash-verified loading, fail-closed

The overlay merge tool produces `.sai-overlay-manifest.json` (SHA-256
of every merged file). The runtime verifier re-hashes on startup. Any
mismatch (tampering, accidental edit, missing file) fails the control
plane before it can do anything. Three modes (`strict`, `warn`, `off`);
production runs strict.

#### 23. Boundary-linter enforced

A pre-commit hook + GitHub Actions check on the public repo catches
real email addresses, personal names, `/Users/...` paths, real Slack
channels, phone numbers, and secret-scheme references (`op://`,
`keychain://`). Per-file allowlist requires a comment justifying the
exemption — no bare allowlist entries.

#### 24. Big changes ship as a sequence

Migrations, refactors, and feature additions ship as a sequence of
focused commits with clear scope, not one mega-change. Each commit
keeps tests green and the boundary linter clean. Per-task migrations
in particular are one task per session — TaskConfig YAML + tier impls +
tests + private factory + smoke + cutover, in order.

---

### Operations

#### 25. Drop, don't delete

Skipped records stay in the audit log with `reason="..."`. Failed asks
become predictions with `metadata.ask_failed=True`. Expired asks get
marked EXPIRED, never auto-deleted. Deprecated workflows get tagged
deprecated and kept for ≥1 month before pruning. "Why didn't this
train?" must be answerable from the JSONL log.

#### 26. Hard ceilings, not queues

Daily Slack-ask budgets are hard caps. When exceeded, the orchestrator
skips with a recorded reason — never queues for tomorrow. Tomorrow's
budget gets fresh priorities based on tomorrow's coverage state.

#### 27. Fault-tolerant cascade

A downstream service failure (Slack down, LLM API timeout, channel
missing, Ollama unreachable, OAuth expired) becomes
`Prediction(abstained=True, metadata={"error_type": ...})`. Cascade
continues per escalation policy. Runs never crash because something
upstream is unreachable.

#### 28. Confirmation + clarification for human asks

When a human replies validly, post a confirmation reply in the same
thread so they know it was received and applied. When the reply is
unrecognized, post a clarification listing valid options; the ask
stays OPEN for the next attempt. Never silently process a reply, and
never auto-act on a reply the parser couldn't validate.

#### 29. Observability is built-in, not bolt-on

Tracing infrastructure (LangSmith or equivalent) ships in public; only
API keys are private. Every cascade run, every Slack ask, every
reconciliation outcome is counted and visible. The framework is
open-source-ready; the operator's specific traces are protected by the
boundary linter (no real customer content in public traces).

#### 30. Test before action; smoke before cutover

Every Tier impl, Provider, parser, and reconciler has unit tests. Every
architectural component has integration tests exercising the narrative
end-to-end with mocks. Before any production change, smoke against a
side-by-side environment for a meaningful window. Rollback must be one
command + one config edit; if it takes longer than that, the change
isn't ready to ship.

---

## What this system is not

- **Not a multi-tenant platform.** Single operator. If a feature only
  makes sense for many users, it doesn't belong here.
- **Not enterprise compliance theater.** Audit and approval are real
  properties for the operator's use, not certifications for an external
  auditor.
- **Not a chat interface for the agent.** Chat surfaces are for design
  conversations. The agent itself runs as a service on the operator's
  machine, invoked by skills/CLI/HTTP.

---

## Operating defaults

These are *defaults*, not durable values — calibrate per-task in the
relevant TaskConfig. Defaults that change globally are rare; treat them
as commitments.

| Concern | Default |
|---|---|
| Tier confidence threshold (rules) | 0.85 |
| Tier confidence threshold (classifier) | 0.85 |
| Tier confidence threshold (local LLM) | 0.70 |
| Tier confidence threshold (cloud LLM) | 0.60 |
| Reality observation window | 7 days |
| Daily Slack-ask budget per task | 5 |
| Graduation experiment sample rate | 0.20 |
| Graduation experiment window | 14 days |
| Cost table currency | USD per 1M tokens |
| Eval channel name | `sai-eval` |
| Hash-verification mode (production) | strict |

---

## Working with Claude on this project

When starting a new Claude session for SAI work:

1. **Read this file first** — saves re-explaining the rules.
2. **Read `MIGRATION-PRINCIPLES.md`** for the current cycle's snapshot,
   priorities, and active task-migration queue.
3. **Pick a discrete scope.** A single task, a single refactor, a single
   sub-step. Not "redo the architecture."
4. **State the principle being applied** when designing a change. Pin
   it to a numbered rule above so the reasoning is checkable.
5. **Test before commit.** Tests stay green. Boundary linter stays clean.
6. **Document deferred work** in the active migration doc, not in
   this file.
7. **Drop, don't delete** when the framework changes shape.

If a proposed change appears to violate a principle, name the principle,
propose the deliberate exception with reasoning, and get explicit
approval. Don't silently work around it.

---

## Glossary

- **SAI** — the framework name.
- **Tier** — one rung of the cascade (rules / classifier / local_llm /
  cloud_llm / human).
- **Provider** — vendor-specific LLM SDK wrapper.
- **Task** — a workflow with its own cascade, eval dataset, and
  preferences. Defined by a TaskConfig YAML + a private TaskFactory.
- **EvalRecord** — one processed task input + its tier predictions,
  active decision, and (eventually) ground truth.
- **Reality** — observed real-world signal. The only source of
  `is_ground_truth=True`.
- **Cascade** — sequential walk through tiers with early-stop on
  confidence.
- **Cutover** — moving production from old runtime path to new via a
  config edit and runtime reload.
- **Overlay** — public + private merge model. Private wins on path
  conflicts.
- **Co-work** — real-time human+SAI session producing recorded
  decisions and proposed preferences.

---

*This document is itself a deliverable. Changes to it require the same
discipline as changes to code: state the principle being added or
modified, propose the wording, get review, commit with a clear message.
Drift between principles and practice is the root cause of every "wait,
why did we do it like this?" question — keep it tight.*
