# SAI core principles

Read this first. These are the durable rules every change in SAI is judged
against — the architectural and operational defaults that don't get
relitigated each session.

If a proposed change violates a principle, name it and propose a deliberate
exception with reasoning. Don't silently drift.

This file holds **durable principles only**. Phase plans, current-cycle
migration sequencing, and "what's done so far" live in
`MIGRATION-PRINCIPLES.md` and `SHIP-READINESS.md` — both in the
operator's private overlay, NOT this public repo. Anything time-bound
or task-specific belongs there.

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

#### 6a. Every input + every output is guarded — schema enforcement at every boundary

Security is the first-order property. **Every value that crosses a
trust boundary** — LLM responses, human replies, network calls,
config file loads, tool outputs, anything the framework consumes
or produces — **MUST be validated against an explicit allowed
shape**. There is no "best-effort interpretation" path. If the
value isn't in the allowed shape, the system either:

  * **Asks for clarification** (when there's a human to ask), or
  * **Escalates** (when there isn't), or
  * **Refuses** (with a clear error logged to the audit trail)

It NEVER silently treats an unrecognised value as "probably what
the caller meant." Guessing is a security failure, period.

**Concrete enforcement points** (this is a non-exhaustive list —
the principle applies to ALL boundaries):

1. **LLM enum outputs** — when an LLM is supposed to return one
   of N values (a verdict, a classification label, an action
   name), the API call MUST use structured-output enforcement
   (Anthropic tool-call with strict JSON Schema, OpenAI
   `response_format=json_schema` with the enum). Don't rely on
   the prompt alone — prompts are guidance; schemas are
   enforcement.

2. **Human approvals** — when a human reply is supposed to be
   "approve / reject / feedback", the parser MUST have a
   canonical set of approve-tokens (`approve`, `yes`, `sg`, `ok`,
   `lgtm`, `✅` etc.) and a canonical set of reject-tokens. Any
   reply that matches NEITHER list is "feedback" (post a
   clarification asking for an explicit ✅/❌, do NOT guess
   intent). The slack_bot's `_classify_reply` is the canonical
   shape.

3. **Tool inputs** — every tool the agent can call validates its
   inputs against a Pydantic model (`extra="forbid"`). Unknown
   keys, wrong types, missing required fields → tool refuses,
   surfaces error to LLM (so the LLM can correct on next turn).

4. **Tool outputs** — tools that return JSON MUST validate the
   output against a Pydantic model before handing it back to the
   cascade. If validation fails, the tool reports the error;
   the cascade escalates rather than passing malformed data
   downstream.

5. **Config file loads** — every YAML/JSON config the framework
   reads goes through a Pydantic model with `extra="forbid"`.
   Unknown keys = silent typos = fail closed (loud error at
   load time, not surprise behavior at runtime).

6. **Network calls** — responses validate against an explicit
   schema (provider response shape, OAuth token shape, Slack
   event shape). Unexpected shape → treat as failure, not
   "let's see what fields ARE there."

**The lazy version of every boundary is "accept anything and
hope for the best." That version is never acceptable.** When
adding a new boundary, the question to ask is "what is the
allowed shape, and what happens if input/output doesn't match?"
If you can't answer the second part, you haven't shipped yet.

**The principle bites in two directions:** input guards prevent
malicious or malformed data from corrupting the system; output
guards prevent the system from leaking unsafe or malformed data
to operators / external services / future cascade tiers. Both
are equally important. A skill that validates inputs but emits
unguarded outputs is half-secured — a future caller will trust
the unguarded value and the chain breaks one hop downstream.

**Concrete v8 lessons (2026-05-04 night):**

- The e1 cornell-delay-triage classifier was returning verdicts
  outside the documented enum (`extension_request`,
  `STUDENT_WELLBEING_CONCERN`, etc.) because the
  `AnthropicJsonProvider` used an open-ended JSON schema.
  Prompt told the model "return one of three strings"; nothing
  enforced it. **Fix:** strict JSON Schema with `enum` on every
  classifier-style LLM call, enforced at the API layer.

- The `slack_bot._classify_reply` already does this right for
  human approvals: APPROVE_TOKENS + REJECT_TOKENS canonical
  sets; everything else routes to "feedback" with a
  clarification reply, never to silent approval.

#### 7. Secrets never live in either repo

`.env` files contain only references — `op://<vault>/<item>/<field>` or
`keychain://<service>/<account>`. Never literal `sk-...`, `xoxb-...`, or
any other vendor token. Real values are pulled at runtime via the
1Password CLI or macOS Keychain bridge.

#### 7a. 1Password access is service-account only — never interactive unlock

Every code path that resolves `op://` references MUST run with
`OP_SERVICE_ACCOUNT_TOKEN` set BEFORE invoking `op`. The operator
should NEVER see a 1Password unlock dialog as a side effect of
running SAI code. This is enforced by:

1. **The wrapper**: ``scripts/with_1password.sh`` is the canonical
   entrypoint for any command needing `op://` secrets. It reads
   `OP_SERVICE_ACCOUNT_TOKEN` from `~/.config/sai/runtime.env`
   (resolved via keychain://), exports it, sets
   `OP_BIOMETRIC_UNLOCK_ENABLED=false` and `OP_CACHE=false`, points
   `OP_CONFIG_DIR` at a fresh tmp dir, then runs the wrapped command.
   The result: `op` runs in pure service-account mode — no biometric
   prompt, no desktop-app integration, no interactive unlock.
2. **The runtime env loader**: ``app/shared/runtime_env.py`` resolves
   `op://` references in-process via the same `op` CLI. It works
   ONLY when `OP_SERVICE_ACCOUNT_TOKEN` is already in the env (which
   `with_1password.sh` guarantees, OR a parent shell has sourced
   `~/.config/sai/runtime.env`). If `op` isn't installed or the
   token is missing, the loader returns None and the consumer's
   missing-secret friendly fallback fires.
3. **Order of secrets in runtime.env**:
   `OP_SERVICE_ACCOUNT_TOKEN="keychain://sai/onepassword_service_account_token"`
   MUST appear before any `op://` reference so it gets resolved
   first. The sequence is keychain → token → op CLI → other secrets.

Direct invocations of `op signin` or interactive auth in operator-
facing scripts are a bug. If you find yourself wanting to call `op`
without service-account auth, route the call through
`with_1password.sh` instead. If you find yourself wanting to write
`op://` references in code (instead of in env files), don't — keep
secret references in `op.env` / `runtime.env` so the boundary linter
+ the wrapper layer catches them.

When this principle is violated the symptom is operator-visible:
they get a 1Password unlock prompt out of nowhere, often as a side
effect of a "test" command. That is a hard bug — fix the call site
to use the wrapper, then add a regression test for the call path.

#### 8. Runtime state outside the repo

`~/Library/Application Support/SAI/state/` for sqlite + checkpoints +
heartbeats. `~/Library/Logs/SAI/` for append-only logs.
`~/.config/sai/tokens/` for OAuth tokens. Working trees stay
code-and-config-only. The boundary linter prevents committing anything
state-shaped.

#### 9. Operator-edit paths are channel-and-pattern locked

The system MAY edit prompt files, rule lists, and policy files in
response to operator instructions, BUT only under all five of the
following conditions simultaneously:

1. **Channel-bound.** The instruction arrives through the eval
   channel (`#sai-eval` by default). Any other channel — DMs, other
   public channels, email, the API — cannot trigger an edit.
2. **Identity-bound.** The replying user_id matches the configured
   operator (validated against `auth.test` once at startup, stored).
   Replies from any other user_id are ignored, including replies that
   appear to be impersonating the operator.
3. **Pattern-bound.** The instruction matches a pre-registered safe
   pattern (rule add, prompt-line edit, threshold tune). Free-form
   "do whatever I say" instructions are not a registered pattern and
   never apply.
4. **Two-phase committed.** Stage one: the parser writes a proposal
   file (`eval/proposed_*.yaml`) with the parsed change. Stage two:
   a separate explicit `/sai-checkin` step applies the change to the
   prompt or policy file, recomputes hashes, runs the regression
   evaluator, and only commits if regression passes. Both stages
   require positive operator action; neither is automatic.
5. **Hash-aware.** After any operator-driven edit, the merged-runtime
   manifest is regenerated and the hash-verifying loader picks up
   the new content on next reload. Tampering is still detectable.

This is a stricter version of #20 (Reflection may suggest, never
auto-apply) tuned to the specific risk that Slack — by being the
human-in-the-loop channel — could become a covert control plane
if any of the five gates loosen.

The system NEVER edits its own files based on:
- Email content (always untrusted; principle #6).
- Replies from unknown users or other channels.
- LLM-generated patches that haven't passed Stage 2.
- Patterns that aren't pre-registered.

---

### Eval & cascade

#### 10. Eval-centric architecture

The system's purpose is to grow a high-quality eval dataset. Every task
input produces one append-only `EvalRecord` partitioned by `task_id`.
Tier predictions live in the record for audit but never count as ground
truth. Cheaper tiers graduate when their precision/recall against
ground-truth records clears the threshold and a human approves.

#### 11. Reality-only ground truth

Three legitimate sources of `ObservedReality`:

- **Direct user action** in the system being mediated (Gmail re-tag,
  calendar event, booking confirmation).
- **Explicit reply to a Slack ask** (typed answer, parsed by the task's
  ReplyParser).
- **Co-work session approval** (real-time human+SAI collaboration that
  produces a recorded decision).

Models agreeing with each other is never ground truth. Even a unanimous
cascade leaves `is_ground_truth=False` until reality confirms.

#### 12. Cascade with early-stop, never parallel

Tasks list tiers cheapest → most expensive. The runner walks them in
order; the first tier whose prediction is non-abstaining AND clears its
confidence threshold resolves the request. Cascade halts. Most requests
resolve in one tier. The expensive tier is the long tail, not the
default.

Never run all tiers on every input "just to compare." Comparison happens
via deliberate, time-bounded `graduation_experiment` blocks at sample
rates of 10–20%.

#### 13. Pluggable Provider abstraction

A `Provider` is the single Protocol that wraps a vendor SDK call:
`predict(LLMRequest) -> LLMResponse`. Switching from OpenAI to Anthropic,
or swapping models, is a YAML edit + a Provider class swap. Never a tier
rewrite.

Cost is the Provider's responsibility — it consults `cost_table.yaml`
(USD per million tokens, per provider+model) and emits `cost_usd` in
every response. Cost rolls up to per-task ROI dashboards automatically.

#### 14. Pluggable factories everywhere

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

#### 15. Sample-rate experimentation

Comparing a candidate cheaper tier against the active tier doesn't run
on 100% of inputs — that defeats the cost goal. `graduation_experiment`
blocks let a candidate shadow-run on a sample (default 10–20%) for a
bounded window (default 14 days). Statistical significance comes from
window length, not sample size.

After the window closes, the GraduationReviewer compares candidate
predictions to ground truth and posts a Slack ask: "Promote? P/R numbers
attached." Human approves; `active_tier_id` flips.

#### 16. Co-work extracts, only approval applies

Real-time human+SAI sessions ("co-work") may produce inferred preferences.
These land as `Preference(strength=PROPOSED, source=COWORK)`. Runtime
ignores PROPOSED preferences. Application requires explicit human
approval flipping the strength to SOFT or HARD.

The PreferenceRefiner watches for violations of active preferences and
proposes refined versions. Same rule: refinements are PROPOSED until
approved. Preferences are never edited silently.

#### 16a. Eval datasets are first-class — every eval surface is an EvalDataset

Eval is the framework's reason to exist (#10). Every eval surface in
the system is an instance of the same abstraction: an
**EvalDataset** bound to a specific evaluation target.

The unified shape (`app/eval/dataset_base.py::EvalDataset`):
- one JSONL file of cases, one Pydantic schema for the case shape
- `load()` / `count()` / `append(case, on_evict)` / `run(evaluator)`
- soft-cap (optional) with eviction strategy
- fail_mode (`hard_fail` / `soft_fail`)
- target_kind (`rules` / `llm` / `workflow` / `safety_gate`)

Five concrete subclasses ship today
(`app/eval/datasets.py`):

- **CanaryDataset** — target=rules, no cap, hard_fail.
  One synthetic case per rule. Regenerated deterministically from the
  rules config. Default file: `eval/canaries.jsonl`.
- **EdgeCaseDataset** — target=llm, soft-cap=50, soft_fail.
  Real emails where the LLM had to reason; operator-confirmed L1.
  When at cap, append() evicts the most-redundant existing row. If
  a TrueNorthDataset is set as the on_evict callback, evicted rows
  are archived (see #16h). Default file: `eval/edge_cases.jsonl`.
- **WorkflowDataset** — target=workflow, no cap, hard_fail.
  Catches drift in a workflow's plumbing (system prompt, regex,
  tool wiring). Per #16d every workflow ships one. Default case
  shape: `WorkflowCase`. Workflows can subclass for richer assertions
  (e.g. sai-eval has its own per-case CaseResult dataclass that
  inherits from the generic shape).
- **DisagreementDataset** — target=llm, no cap, soft_fail.
  Local-vs-cloud disagreements awaiting Loop 2 batch surfacing.
  Drained when the operator resolves the batch.
- **TrueNorthDataset** — target=llm (or workflow-specific), no cap,
  soft_fail. Append-only historical record. See #16h.

The framework provides ONE runner (`EvalDataset.run(evaluator)`) and
ONE report shape (`DatasetReport` with `CaseResult[]`). Workflow-
specific assertions go in the `evaluator` callable that the workflow
provides.

**The four loops that maintain them** (unchanged in semantics; just
retargeted onto the unified `EvalDataset` abstraction):

1. **Loop 1 — Pre-ship regression (coder gate).** Triggered by any
   code edit affecting classification (rules YAML, prompts, model
   swap, classifier logic). Runs in cascade order: canaries first
   (fail-fast), then LLM edge cases (P/R/F1, alert on degradation).
   Blocks ship on canary fail or material P/R drop. Consumer: the
   coder, every time. Operator only sees results on failure.

2. **Loop 2 — Disagreement triage (operator batched ask).** Runtime
   architecture: local and cloud LLM run in parallel. On
   disagreement, cloud wins as the runtime tiebreaker — but cloud
   is *not* assumed correct in resolution. The disagreement triggers
   a writeback to dataset C. When C accumulates ≥
   `DISAGREEMENT_BATCH_THRESHOLD` (default 50) unresolved rows, a
   curator picks the most-informative N and posts ONE batch ask in
   `#sai-eval`. Never per-email. Consumer: the operator, when they
   choose to engage. Operator verdicts are the ground truth.

3. **Loop 3 — Resolution → code change → regression → witness.**
   Triggered by Loop 2 batch coming back resolved. Look at verdict
   patterns; adjust code (rules YAML edit, prompt addendum); run
   Loop 1; add the *witnesses* — the subset of resolved
   disagreements that best capture the lesson — to the appropriate
   dataset (A if rule, B if LLM hint). Not all resolved
   disagreements; only canonical witnesses. Consumer: nobody
   (autonomous), full audit log.

4. **Loop 4 — Operator-driven rule change (ad-hoc).** Operator says
   in `#sai-eval` either "rule: from <X> → L1/Y" (classifier change)
   or "<X> should have been L1/Y" (LLM hint). Same downstream as
   Loop 3: code adjustment → Loop 1 → add witness. Consumer: nobody
   after operator triggers.

**Loop 1 is the universal gate.** Every code change goes through
it. Loops 2, 3, 4 are the *sources* of code changes. The regression
sets grow by exactly one curated witness per change — never by
schedule, never by bulk import.

**Promotion rule (Loop 3 / Loop 4):** when a resolution produces a
new fixed rule:
1. Append the rule to the rules config; regenerate dataset A.
2. Sweep dataset B for any rows the new rule covers at ≥ production
   confidence. Remove them from B (now redundant — A covers them).
3. Add the witness to A only; do NOT also add to B.

Net direction: B shrinks over time as the rules tier absorbs more
cases. The aim is rules-handle-everything; the LLM is the residual.

**Size cap on B:** soft-capped at `EDGE_CASE_SOFT_CAP` (default 50).
Loop 3 / Loop 4 must evict the most-redundant row in the same
bucket if B is at cap before adding a new witness. Hard size
discipline keeps the regression fast and forces real curation.

**No backlog files for rule conflicts.** When curation surfaces a
row where a rules-tier rule fires confidently (≥ production
threshold) but conflicts with an operator label, the resolution is
*immediate*: edit the rule (Loop 4) or discard the row because the
rule is right and the label was wrong. Persistent "pending rule
review" files are explicitly disallowed — they accumulate stale
decisions and add no value.

#### 16b. Slack `#sai-eval` is the operator's feedback channel

Operator-driven changes (Loops 2 and 4) arrive through `#sai-eval`.
The five gates of principle #9 apply in full (channel-bound,
identity-bound, pattern-bound, two-phase committed, hash-aware).
Pre-registered patterns:

- `add rule: from <sender|domain> → L1/<bucket>` — Loop 4
  classifier change. Stages a proposal at
  `eval/proposed_rule_add_<ts>.yaml`. Pre-commit guard runs
  Loop 1 against the proposed rule; if any existing dataset row
  flips, commit blocks and the conflict surfaces.
- `<message_ref> should have been L1/<bucket>` — Loop 4 LLM hint.
  Stages a proposal at `eval/proposed_eval_add_<ts>.yaml`. Applies
  on `/sai-checkin` after Loop 1 passes.
- (Loop 2 batch ask reply) — operator verdicts on the surfaced
  disagreements. Triggers Loop 3 once the batch resolves.

The CSV path (operator reviews a file in `~/Downloads/`, runs
`sai eval add-edge-case --from-csv`) is the v1 fallback while the
Slack-native pattern parsers + `/sai-checkin` ship in Phase 4.

This channel is **guarded** per principle #16e: anything that isn't a
registered pattern gets a friendly out-of-scope reply (never silence).

#### 16i. Each guarded interface registers what it's ALLOWED to discuss

Every guarded interface (Slack channel, web page, future WhatsApp,
HTTP endpoint, etc.) declares — in a single registry the operator
can audit — what topics it can discuss + what risk class each topic
carries. **Default for new interfaces: empty allowlist (refuse
everything).** Adding a topic requires an explicit registry entry.

The runtime checks the registry on EVERY message before invoking
any tool. If the message's intent doesn't map to an allowlisted
topic for that interface, the bot refuses with a friendly redirect
(per #16e — never silent).

**Concrete v8 implementation:**

The registry lives at ``config/channel_allowed_discussion.yaml`` —
gitignored copy in private overlay (operator-specific channel names),
public template for stranger installations. Editable ONLY via
Claude Code (no Slack, no web). Format:

```yaml
sai-eval:
  description: "Operator-driven taxonomy + LLM eval feedback"
  allowed_topics:
    - kind: classifier_rule_change       # add/remove sender→bucket rules
      risk_class: low                    # rules tier; canary-gated
      tools: [propose_classifier_rule]
    - kind: llm_example_addition         # add one-off teaching example
      risk_class: low                    # edge_cases; soft-fail-gated
      tools: [propose_llm_example, search_gmail, read_message, read_thread]
    - kind: query_label_state            # read-only "what labels exist?"
      risk_class: minimal
      tools: [list_gmail_labels]

sai-cost:
  description: "Cost reporting + budget queries (read-only)"
  allowed_topics:
    - kind: cost_query
      risk_class: minimal
      tools: [read_audit_log, format_cost_report]

sai-metrics:
  description: "Eval / regression / quality metrics (read-only)"
  allowed_topics:
    - kind: metric_query
      risk_class: minimal
      tools: [read_audit_log, read_eval_metrics, format_report]

# New channels start with allowed_topics: [] — refuse everything
# until the operator explicitly adds entries.
```

**Risk classes** map to gating policy:
- `minimal` — read-only; no second-opinion gate needed
- `low` — propose-only with two-phase commit; existing apply gate
- `medium` — propose-only + second-opinion gate (#16f) on local LLM
- `high` — propose-only + second-opinion gate on cloud LLM + operator approval
- `forbidden` — explicitly NOT allowed; refuse + log

The bot's per-message flow becomes:
1. Identify channel + parse operator intent (regex or agent)
2. Look up channel in registry
3. If intent maps to an allowlisted topic → proceed with that topic's
   tools only
4. Else refuse + log to ``denied_attempts.jsonl``

**Why this matters:** the slack-eval agent today has hard-coded
tools (search_gmail, propose_*, etc.). When new channels ship
(sai-cost, sai-metrics, future), each should NOT inherit the eval
agent's full tool surface. The registry is the answer to "what
specifically is this channel allowed to do?" — auditable in one
file, NOT scattered across handler code.

**Process rule:** ANY change to the registry creates a Loop 4
proposal that runs the same regression gate as any other workflow
edit. Adding a new allowed_topic IS a state mutation; it goes
through the two-phase commit.

#### 16h. True-North dataset — capped working set + uncapped historical record

Each workflow keeps TWO datasets in parallel for ground-truth:

1. **Working edge_cases** (`eval/edge_cases.jsonl`) — soft-capped at
   `EDGE_CASE_SOFT_CAP` (50). Run on EVERY change as the regression
   gate. Cheap, fast, focused on representative cases. Per #16a
   the soft-cap forces curation discipline.

2. **True-North** (`eval/<workflow_id>_true_north.jsonl`) —
   **uncapped**. Every operator-approved case the system has ever
   seen, append-only. Run OCCASIONALLY for a full-fidelity
   completion check (manual invocation OR a weekly cron). Doesn't
   gate every change.

**Why two datasets:** the working set is a fast feedback loop on
every change. The True-North is the long memory — the basis for
"are we as good as we used to be on the entire history?" — and the
training corpus for future cheap-tier graduations (#15).

**How rows move:**

- Operator approves a Loop 4 proposal → the row goes into BOTH
  working edge_cases AND True-North (assuming the eval contract
  applies — `eval_add` proposals already do this for working).
- When the working soft-cap evicts a redundant row (per #16a
  promotion rule), the evicted row is APPENDED to True-North
  (NOT deleted). The working set shrinks; the historical record
  grows.
- When a Loop 3 batch resolves → witnesses go to BOTH working
  edge_cases AND True-North.
- True-North rows are NEVER edited or deleted — append-only audit
  shape. If a row turns out to be wrong, append a CORRECTION row
  with `supersedes: <old_id>` so the lineage is preserved.

**File shape:** same `EdgeCaseRow` schema as `edge_cases.jsonl`,
plus optional fields:
- `archived_from_working_at: datetime` (set when promoted from
  capped working set)
- `supersedes: Optional[str]` (when a correction row replaces an
  earlier verdict)

**Cost discipline:** True-North runs are NOT cheap. A 500-row
True-North checked against the cloud LLM tier costs ~$2-5. Run
manually OR on a weekly schedule with a hard cost cap. Default
cron: weekly Sunday 3am with `SAI_TRUE_NORTH_MAX_COST_USD=2.00`.

**Per-workflow scope:** every workflow under §33 ships its own
True-North file. Operator's e1, e2, future skills all build their
own historical record as they accumulate feedback. The skill
manifest's `eval` slot gains an optional `true_north` sub-spec:

```yaml
eval:
  canaries: ...
  edge_cases: ...
  workflow_regression: ...
  true_north:                                # OPTIONAL but recommended
    path: <workflow_id>_true_north.jsonl
    weekly_check: true                       # cron Sunday 3am
    max_cost_per_check_usd: 2.00
```

**Eval contract gating:** True-North is OPTIONAL in the manifest
(unlike the three required slots) because new workflows start with
zero historical cases. After a workflow has been live for ~30 days
+ accumulated >25 approved corrections, the operator should opt
into True-North. The framework's onboarding wizard reminds them at
that point.

#### 16g. Operator triggers create pending intents — never drop silently

When an operator opens an interaction with a guarded interface
(types in `#sai-eval`, sends an HTTP chat message, etc.), the
trigger creates a **pending intent** with status=open. The intent
persists in memory + on disk until ONE of these closure events:

1. The operator approves a proposal under the intent (✅) →
   status=resolved
2. The operator explicitly drops the intent ("never mind", "drop
   it", `--cancel`) → status=dropped
3. The intent goes idle past `INTENT_IDLE_TIMEOUT_HOURS` (default
   24h) → status=expired with a final operator-visible note

**The intent is NEVER dropped by:**
- A rejection reaction (❌) on a staged proposal — that's MID-FLIGHT
  feedback, not closure. The bot must respond with "got it — what
  shape would be right?" and listen for the next reply.
- The bot deciding the operator was unclear — ask again, do not
  walk away.
- A failed apply gate (regression rolls back) — surface the failure
  + ask if the operator wants to try a different shape.
- The agent reaching iteration cap on one turn — that's ONE turn's
  budget. The intent continues across turns.

**The history of an intent is a record:** every proposal staged,
every rejection (with the operator's reason), every operator
comment, every approval. When the agent re-invokes under an open
intent, the history is part of the agent's input — so "we proposed
X, you said no because Y" becomes context for the next attempt.

**Why this matters:** the operator's job is the workflow they're
trying to express, not running our state machine. If they typed
"this email should be Finance" and the bot misread it as "rule on
sender", the operator should be able to clarify in the same thread
("no, just this one email") and have the bot fix its proposal —
not have to retype the whole intent or live with a wrong rule.

**Implementation surface:**
- `app/eval/pending_intents.py` owns the schema + store
- The Slack bot opens an intent on each top-level operator message,
  appends events on each interaction, and resolves/drops on closure
- The agent's system prompt knows about prior attempts and is
  instructed to propose a DIFFERENT shape when rejected with a
  reason

**Eval contract:** when an intent fails to reach closure (expires
idle, or the operator force-drops with `:no_entry:`), that goes
into `slack_eval_canaries.jsonl` as a workflow regression case the
agent must learn to handle. The bot itself is a workflow under
§16d; its eval set tracks resolution rates.

This is distinct from #28 (hard ceilings, not queues): hard
ceilings apply to *daily ask budgets* — capacity. Pending intents
apply to *individual operator interactions* — completeness. An
intent always reaches closure (one of the three statuses); the
ceiling controls how many intents we can have open at once.

#### 16f. Agent execution planes — guardrails-as-tools

When an interface needs LLM flexibility (free-form input, ambiguous
intent, conversational repair) the right shape is **an agent with a
guarded tool surface**, not a rigid LLM-as-parser. The cascade
pattern (#12) applies: rules tier first (regex / deterministic) for
the simple fast path, agent tier as the fallback when rules abstain.
Both feed the same two-phase commit (#9).

The execution plane has four required elements:

1. **Tool surface = guardrail.** The agent can ONLY do what its
   registered tools allow. Two rights tiers:
   - `read_only` — no state mutation (e.g., search Gmail, list labels)
   - `propose_only` — writes a YAML proposal under `eval/proposed/`;
     mutation requires the operator's ✅ via the existing two-phase
     commit. The agent NEVER mutates production state directly.
2. **Surface declaration.** Each tool's rights, blast radius, input
   check, and output check are declared in a single source-of-truth
   file (e.g., `app/agents/sai_eval_agent.surface.yaml`). Adding a
   tool requires updating BOTH the declaration AND the registered
   implementation; the agent runner reads the registered set and
   constructs the LLM-side schema at startup.
3. **Supervisory layer.** Iteration cap (`MAX_ITERATIONS`), per-
   invocation cost cap, audit log row per invocation. Tool failures
   surface back to the LLM as tool_results so it can adjust (e.g.,
   "L1/keynote isn't a Gmail label yet — pick from {customers,
   partners}"). Hard refusals (state-mutating tool requests, runaway
   loops, bucket-not-in-Gmail-labels) terminate cleanly.
4. **Vocabulary discovered at runtime.** Where applicable, the agent
   reads live system state via tools instead of hardcoded enums. For
   the sai-eval agent: bucket vocabulary comes from
   `list_l1_labels()` reading the operator's Gmail L1/* labels —
   adding a Gmail label adds a bucket. Tools refuse propose_* with
   buckets not in the live label list.

Distinct from #20 (reflection may suggest, never auto-apply): #20
governs proposed *prompt/policy* edits; this principle governs the
shape of any guarded LLM execution plane that takes operator input.

The pattern generalises beyond Slack — any trigger that needs LLM
flexibility (HTTP endpoint, scheduled re-evaluation, future skill-
control surfaces) follows the same shape: cascade with rules first,
agent with guarded tools as fallback, supervisory caps, audit log,
two-phase commit on any mutation.

Concrete v8 implementation: ``app/agents/sai_eval_agent.py`` is the
execution plane behind ``#sai-eval``. Surface declared in
``app/agents/sai_eval_agent.surface.yaml``. Tool implementations in
``app/agents/tools.py``. Tests at ``tests/test_sai_eval_agent.py``
and ``tests/test_sai_eval_tools.py``.

#### 16e. Guarded interfaces never stay silent

Every interface that accepts operator input through a named channel
(Slack channel, HTTP endpoint, future skill-control surfaces, etc.)
is **guarded**: it accepts only a documented set of intents and
refuses everything else. Refusal is **never silent** — the bot
replies with a friendly explanation of what the interface is for and
what intents it currently accepts. The operator might be testing,
warming up, or just curious; silence is ambiguous (did the bot see
me? crash? choose to ignore?). A reply removes the ambiguity AND
doubles as discoverability — the operator never has to read docs to
learn what's available.

Three properties every guarded-interface refusal must satisfy:

1. **Acknowledge the input.** The reply tells the operator they were
   heard, not that they were rejected. "That's outside what I do
   here" is right; "ERROR: command not recognized" is wrong.
2. **List current capabilities.** Source the list from a single
   in-code constant so capability + reply stay in sync as the
   interface grows new intents.
3. **Stay friendly, not robotic.** Out-of-scope inputs are
   conversational, not errors. Match that tone.

Distinct from principle #30 (which governs replies to known asks):
this principle governs *unrecognised* top-level inputs on a guarded
channel.

Concrete v8 implementation: `#sai-eval` accepts (a) classifier rule
changes and (b) one-off LLM example corrections. A "tell me a joke"
message gets back something like: "That's outside what I do here —
this channel is just for evaluation feedback. Right now I can take:
• Add a classifier rule — `add rule: …` • Mark one specific email —
`… should be …`."

#### 16d. Every workflow gets the same shape — no exceptions

The framework's reason to exist is to make any AI workflow plugged into
it automatically inherit the cascade + eval + feedback + safety
infrastructure. To enforce that, every workflow declared via the
plug-in protocol MUST define all of these slots:

**Workflows include the LLM-driven control surfaces that operate on
other workflows.** The sai-eval agent (which proposes changes to the
classifier rules + LLM eval set) is itself a workflow with its own
canaries — `slack_eval_canaries.jsonl` exercises "tell me a joke",
"<reference> should be <label>", and bucket-doesn't-exist cases.
Changing the agent's system prompt, tool surface, or regex tier
re-runs that regression set BEFORE ship, exactly the same way
classifier rule edits re-run the classifier canaries. **No workflow
is exempt from owning its own eval set.**

```
WORKFLOW = {
  inputs:    how it's triggered (email pattern / schedule / manual)
  cascade:   ordered tiers (rules / local / cloud / human)
  eval:      datasets: a list of EvalDataset specs (#16a)
             — REQUIRED: canaries + edge_cases + workflow
             — OPTIONAL: true_north (#16h) + disagreement_queue
  feedback:  add_rule + add_eval Slack patterns (auto, plug into #sai-eval)
  outputs:   what it produces (label / draft / send / escalate / many)
  policy:    sending allowed? to whom? approval-required intents?
}
```

A workflow that doesn't declare a `canaries`, `edge_cases`, AND
`workflow` dataset **cannot ship**. The skill-creator (Co-Work skill)
refuses to emit artifacts without them; the framework's loader
refuses to register a manifest without them. This is the
regression-free guarantee.

The cascade tiers are configurable per workflow — a deterministic
workflow may use `cascade: [rules]` only; a draft-generation workflow
may use `cascade: [local_llm, cloud_llm, human]`. But the eval
contract is non-negotiable.

#### 16c. Cloud is the runtime tiebreaker, never silent ground truth

Cloud LLM wins runtime disagreements with local — but only as a
mechanical tiebreaker, never as ground truth. Every disagreement
captured at runtime writes to dataset C. The local LLM does NOT
auto-update its prompt addendum from cloud's choices on a
schedule. Improvement only flows through Loops 2 → 3 (or Loop 4):
operator-confirmed verdicts feed adjustments; Loop 1 protects
against regression; witnesses preserve the lesson.

Background scheduled jobs that update prompts, rules, or eval data
without operator confirmation are explicitly disallowed. The
graduation evaluator (principle #15) is the only exception — it
proposes, but principle #20 (reflection may suggest, never
auto-apply) still gates the application.

---

### Engineering discipline

#### 17. Public ships mechanism. Private ships values.

Mechanisms (validation, parsing, cascading, reconciling, persistence,
verification) are open-source-ready public framework. Values (the actual
taxonomy, the actual prompts, OAuth tokens, channel names) live in the
operator's private overlay.

Test for the split: would you ship this file in an open-source starter
to a stranger who's never seen the operator's data? If yes → public.
If no → private. If you're not sure, it's private.

#### 18. File-level override only in the public/private overlay

The overlay merge writes both repos into one runtime tree, with private
winning on path conflicts. **No deep YAML merging.** If private has
`workflows/x.yaml`, it replaces public's `workflows/x.yaml` entirely.
Per-key merging silently changes behavior; replacement is auditable.

#### 19. Smallest correct scope

Every workflow lists the exact tools it can call. Adding a tool requires
editing its policy file, which is a reviewable diff. Every connector
gets the narrowest API scope it needs. Don't grant blanket capabilities
"in case." Add specifically when needed.

#### 20. Reflection may suggest, never auto-apply

The system can propose prompt or policy improvements (and should). It
**cannot apply them**. Application requires a human-driven check-in
path with hash stamping and tests. Auto-applying changes from the same
agent that observes their effect is the trust failure that ends careers.

#### 21. No surface certifies its own deployment

The surface that drafts a change is never the surface that approves it
for production. Different roles, different tools, different sessions.
This rule is what makes the audit trail mean something — the writer
and the deployer are separated by intent.

#### 22. Naming hygiene

"SAI" is the framework. `sai-email` is a channel/workflow. `sai-run`
is the bridge skill. `sai-eval` is the human-feedback channel. Don't
collapse these. When you introduce a new namespace component, prefix it
clearly and document it in this section's glossary.

#### 23. Hash-verified loading, fail-closed

The overlay merge tool produces `.sai-overlay-manifest.json` (SHA-256
of every merged file). The runtime verifier re-hashes on startup. Any
mismatch (tampering, accidental edit, missing file) fails the control
plane before it can do anything. Three modes (`strict`, `warn`, `off`);
production runs strict.

#### 24. Boundary-linter enforced

A pre-commit hook + GitHub Actions check on the public repo catches
real email addresses, personal names, `/Users/...` paths, real Slack
channels, phone numbers, and secret-scheme references (`op://`,
`keychain://`). Per-file allowlist requires a comment justifying the
exemption — no bare allowlist entries.

#### 24a. Open framework, single API surface

The framework prefers **open, vendor-portable abstractions** and a
**single API surface** for the operator, in that order:

1. **Open frameworks over single-vendor SDKs.** Where a mature open
   framework wraps multiple vendors with one interface (LangChain,
   LiteLLM, OpenLLM), prefer it over a vendor-specific SDK. Operators
   can swap vendors via config; they're never locked to one provider's
   client library inside our code.
2. **One API key for the operator if at all possible.** When the
   operator already has a primary LLM credential (because Co-Work uses
   Claude, or because they've standardised on OpenAI), the framework
   defaults to that same provider for its internal agents. Adding a
   second API key for "an internal classifier" is operator friction
   and a separate billing surface to manage. Use what they have.
3. **Vendor-specific paths are escape hatches, not defaults.** A
   model-specific quirk (gpt-5 doesn't accept `temperature`, harmony
   reasoning channels for gpt-oss) lives in the Provider class as a
   workaround; the cascade and the agent runners stay vendor-agnostic.

Concrete v8 implementation: the sai-eval agent runs on LangChain +
the operator's chosen Claude API (`langchain-anthropic`). LangChain
gives us swap-out to OpenAI/Gemini/local Ollama by changing one line.
LangChain is also what SAI was originally built on — keeping the
framework choice consistent across the codebase.

This principle applies in both directions:
- For operator-facing tools (the agent in `#sai-eval`, future
  HTTP-chat fallback): default to the operator's existing API.
- For internal cascade tiers (cloud_llm in email classification):
  follow the operator's deployment choice; switching providers must
  be a YAML edit, not a code rewrite.

#### 24b. LLM choice is configurable — no hardcoded model names

Every LLM call passes through a Provider (#13) bound to a (vendor,
model) pair pulled from a **central LLM registry** at
``config/llm_registry.yaml``. Code references LLMs by **logical
role** (e.g. `agent_default`, `safety_gate_high`,
`cascade_cloud_fallback`) — never by literal model id.

The registry maps roles to concrete (vendor, model) per **tier**:
- `low` — cheapest acceptable model (e.g. claude-haiku-4-5)
- `medium` — local model preferred (e.g. qwen2.5:7b via Ollama)
- `high` — best available (e.g. claude-sonnet-4-6 or gpt-5)

**Tier policy by surface:**
- **Internal recurring tasks** (cron-fired classification, scheduled
  workers): try LOCAL first (free), escalate to CLOUD only on
  abstain/uncertainty. Per #12 cascade.
- **Human-facing interactive** (sai-eval agent, second-opinion gate,
  any operator-typed-message handler): default to CLOUD MEDIUM
  (claude-haiku-4-5) — operators wait for replies; latency + quality
  matter more than cost. Cost cap per #16f keeps it bounded.
- **Safety-critical** (second-opinion on `high` risk-class outputs):
  CLOUD HIGH (claude-sonnet-4-6 or equivalent). Per #19 smallest
  correct scope — only this surface uses it.

Switching providers / models is a YAML edit + a process restart.
Never a code change. Tests stub the Provider; production reads
the registry. New LLM vendors register a Provider class + a
registry entry.

**Hard rule:** any new module that calls `from openai import …` /
`from anthropic import …` / etc. directly without going through a
Provider is a #13 + #24b violation. Refactor before merging.

#### 24c. Prompts are content-addressed — every load verifies a hash

Every prompt the framework loads passes through the hash-verifying
loader (#23). This applies to:
- Tier prompts (cascade tier prompts in `prompts/`)
- Agent system prompts (today: hardcoded in `sai_eval_agent.py` —
  needs migration; #24c forces this)
- Second-opinion gate `criteria_prompt` (per #16f)
- Skill-defined prompts (any skill manifest declaring a prompt path)

The lock file (``prompts/prompt-locks.yaml``) holds
``<relpath>: <sha256>``. The loader reads the prompt, hashes it,
fails closed if mismatch. Keeps tampered / out-of-date / accidental
edits from silently affecting model behavior.

**Operational rule:** every prompt edit MUST be accompanied by a
prompt-lock refresh. Today this is manual (caught by hash drift
alerts in cron logs); planned: pre-commit hook auto-refreshes (LOOSE-
ENDS L1 / audit A2). Until then, a stale hash is a fail-closed
failure on next reload — operator-visible.

**Where prompts live:** in PRIVATE overlay (per #17 — values are
operator-specific). PUBLIC ships only the LOADER + the schema.
Stranger installs get a `.example` template they fill in.

**System-prompt migration:** the sai-eval agent's system prompt is
currently inline Python (string constant in `sai_eval_agent.py`).
Per #24c that's a violation — the prompt should live in
`prompts/agents/sai_eval_agent.md` with a hash entry. Tracked as
LOOSE-ENDS item (next session work).

#### 25. Standard libraries before custom code

**Before building infrastructure, check if a standard library covers
80% of the use case.** Slack integration, HTTP retry, scheduling,
queues, secret stores, LLM provider abstractions, YAML editing — all
of these have mature libraries with thousands of users who have
already found and fixed the edge cases.

The check, before writing more than ~50 lines of new infrastructure:
*would a stranger reading this file think "wait, why didn't they just
use X?"* If yes — use X.

Custom infrastructure accumulates hidden bugs (silent error swallows,
de-duplication gaps, format mishandling, concurrency races) that
libraries handle by default. A 600-line custom Slack polling daemon
shipped 7 bugs in 2 hours; the same task in `slack-bolt` is 30 lines
of handler code with zero of those bug classes possible.

**Two legitimate exceptions:**

1. **Tiny + well-understood**: when the surface area is small, the
   semantics are clear, and the library brings excess scope. Example:
   `hashlib.sha256` over a manifest is 30 lines and exact-fit; a
   "manifest verification framework" library would be over-engineered.

2. **Public/private boundary requires it**: when our framework's
   abstraction has operator-specific extension points the library
   doesn't model. Example: `Provider` Protocol exposes per-provider
   cost tables and per-tier confidence thresholds; we may *back* it
   with a library like LiteLLM but the abstraction stays ours.

**When you build custom anyway, document the rationale in the file's
docstring**: a "Why not <library>:" paragraph that names the trade-off.
Otherwise the next session will re-litigate the decision.

When this principle is violated, the cost compounds: each new feature
on top of custom infrastructure is more expensive to build and more
likely to expose another lurking bug. Refactor before you add.

#### 26. Big changes ship as a sequence

Migrations, refactors, and feature additions ship as a sequence of
focused commits with clear scope, not one mega-change. Each commit
keeps tests green and the boundary linter clean. Per-task migrations
in particular are one task per session — TaskConfig YAML + tier impls +
tests + private factory + smoke + cutover, in order.

---

### Operations

#### 27. Drop, don't delete

Skipped records stay in the audit log with `reason="..."`. Failed asks
become predictions with `metadata.ask_failed=True`. Expired asks get
marked EXPIRED, never auto-deleted. Deprecated workflows get tagged
deprecated and kept for ≥1 month before pruning. "Why didn't this
train?" must be answerable from the JSONL log.

#### 28. Hard ceilings, not queues

Daily Slack-ask budgets are hard caps. When exceeded, the orchestrator
skips with a recorded reason — never queues for tomorrow. Tomorrow's
budget gets fresh priorities based on tomorrow's coverage state.

#### 29. Fault-tolerant cascade

A downstream service failure (Slack down, LLM API timeout, channel
missing, Ollama unreachable, OAuth expired) becomes
`Prediction(abstained=True, metadata={"error_type": ...})`. Cascade
continues per escalation policy. Runs never crash because something
upstream is unreachable.

#### 30. Confirmation + clarification for human asks

When a human replies validly, post a confirmation reply in the same
thread so they know it was received and applied. When the reply is
unrecognized, post a clarification listing valid options; the ask
stays OPEN for the next attempt. Never silently process a reply, and
never auto-act on a reply the parser couldn't validate.

#### 31. Observability is built-in, not bolt-on

Tracing infrastructure (LangSmith or equivalent) ships in public; only
API keys are private. Every cascade run, every Slack ask, every
reconciliation outcome is counted and visible. The framework is
open-source-ready; the operator's specific traces are protected by the
boundary linter (no real customer content in public traces).

#### 32. Test before action; smoke before cutover

Every Tier impl, Provider, parser, and reconciler has unit tests. Every
architectural component has integration tests exercising the narrative
end-to-end with mocks. Before any production change, smoke against a
side-by-side environment for a meaningful window. Rollback must be one
command + one config edit; if it takes longer than that, the change
isn't ready to ship.

#### 33. Skill plug-in protocol — every workflow declares the same shape

Every workflow plugged into SAI ships as a **skill** with a single
declarative manifest (`skill.yaml`) that the framework loads, validates,
and registers. The manifest is the contract: if it validates, the
workflow inherits cascade + eval + feedback + observability + safety
infrastructure for free. If it doesn't validate, the framework refuses
to register it.

The manifest declares the following slots, all required unless marked
optional:

```
identity:        workflow_id, version, owner, description
trigger:         what fires it (email_pattern / schedule / manual /
                 slack_message / http_webhook / claude_tool)
cascade:         ordered tiers (rules / classifier / local_llm /
                 cloud_llm / agent / human) with confidence_threshold
                 and per-call cost_cap each
tools:           if any tier is `agent` — the bounded tool surface
                 with rights (read_only / propose_only /
                 mutate_with_approval), blast_radius, input_check,
                 output_check per tool
eval:            datasets: a list of EvalDataset specs (#16a). REQUIRED
                 kinds: canaries, edge_cases, workflow. OPTIONAL: true_north,
                 disagreement_queue. Each spec is a discriminated union by
                 `kind` field carrying kind-specific options
                 (cap, metric, max_p_r_drop, run_cadence, etc.).
feedback:        Slack channel + accepted patterns
                 (defaults to #sai-eval + add_rule/eval_add)
outputs:         each side-effect (label / reply / draft / send /
                 post / propose / none) and whether it requires
                 approval (#9 two-phase commit)
policy:          approval_required, cost_cap_per_invocation_usd,
                 iteration_cap, daily_invocation_cap, audit_log_path
observability:   langsmith_project (optional), metrics_emit
```

**The hard contract** (what the framework REFUSES to register):

1. Missing one of the three required eval dataset kinds in
   `eval.datasets`: `canaries`, `edge_cases`, `workflow`
   (#16a + #16d every workflow same shape)
2. Tool surface declares `propose_only` rights but no two-phase
   commit path in policy (#9)
3. `mutate_with_approval` tool without `policy.approval_required:
   true` (#9)
4. Cascade with no `human` tier when `outputs[].side_effect` includes
   `send` or `post` and no `requires_approval` (#19 smallest correct
   scope)
5. Eval files exist on disk but row count < `min_count`

**The soft contract** (warnings, not refusal):
- Cost cap > $1/invocation (asks operator to confirm via the
  control plane before first run)
- Daily invocation cap > 1000 (asks operator)
- Tools using `claude.com` / `openai.com` / etc. without a Provider
  abstraction (#13)

**Why this exists.** Without the protocol, every new workflow
(operator's e1, e2, future) re-invents the surrounding plumbing
(eval files, audit log, regression hook, feedback channel). With the
protocol, the operator's skill-creator (a Co-Work skill) just emits
a manifest + the skill code, the framework wires everything else.
Per principle #14 (pluggable factories) — the skill is the value,
the framework is the mechanism.

**Where it lives.**
- Manifest schema: `app/skills/manifest.py` (`SkillManifest` Pydantic
  model — extra=forbid)
- Loader + validator: `app/skills/loader.py`
  (`load_skill_manifest(path)` and `validate_skill_manifest(manifest,
  skill_dir)`)
- Sample synthetic skill: `app/skills/sample_echo_skill/` —
  demonstrates the protocol without any real Gmail/Slack/Anthropic
  dependency

**Promotion path.** The legacy `WorkflowDefinition` schema (in
`app/shared/models.py`) stays as the runtime workflow loader for
the three demo workflows + the email cascade. New workflows go
through the skill protocol. After ~3 workflows shipped via the new
protocol, deprecate the legacy schema in a focused refactor (per
#26 big changes ship as a sequence).

#### 33a. Skills compose — framework primitives are separate work

A skill is a **declarative composition** of existing framework
primitives. A skill MUST NOT introduce a new framework primitive
inline. New primitives — RAG retrievers, new LLM Provider classes,
new tier kinds, new gating mechanisms, new Pydantic schema types
that other skills could reuse — go through their own design +
review + tests + ship cycle. Only AFTER they exist can a skill
reference them.

**What counts as composition (skill-acceptable):**

- New YAML config values (a new course in `courses.yaml`, a new
  channel in `channel_allowed_discussion.yaml`, a new role in
  `llm_registry.yaml`)
- New hash-locked prompt files (under `prompts/` with a lock
  entry)
- New entries in canonical-memory files (TAs, courses, crisis
  patterns, sender-validation domains)
- Skill-specific deterministic logic in the skill's `runner.py`
  (regex rules, body-template strings, classification → output
  mappings) — small, focused, doesn't generalize beyond the skill
- New `SkillManifest` declarations (cascade tier list, tool list,
  output list) using existing `TierKind` / `ToolRights` /
  `SideEffect` enums

**What counts as a NEW framework primitive (skill-FORBIDDEN —
needs its own dedicated work):**

- A RAG retriever / vector store / embedding pipeline
- A new LLM Provider class (a new vendor or a non-trivial wrapper
  around an existing vendor SDK)
- A new tier kind (anything beyond `rules` / `classifier` /
  `local_llm` / `cloud_llm` / `agent` / `second_opinion` /
  `human`)
- A new policy / gating mechanism beyond two-phase commit + the
  second-opinion gate
- A new public Python module importable by other skills
  (`app/canonical/<new>.py`, `app/cascade/<new>.py`, etc.)
- A new `extra="forbid"` Pydantic schema other skills could reuse

**The test:** would a stranger reading the skill's `runner.py`
think "this code only makes sense for THIS skill" or "this looks
like infrastructure other skills will need"? If the latter — stop
the skill work, write a framework design doc, ship the primitive
on its own, then return to the skill.

**Why this matters:**

1. **Generalizability.** When skill #2 needs RAG, it must be the
   same RAG that skill #5 will need. Inlining means N divergent
   implementations.
2. **Testability.** Framework primitives have unit tests; skills
   have eval contracts. Mixing them collapses both.
3. **Auditability.** Per #4 the audit log is the answer to "what
   did the system do." If every skill's runner has its own audit
   row format, the rollup dashboards (sai-cost / sai-metrics)
   can't aggregate.
4. **Security.** A new gating mechanism shipped inline in a skill
   bypasses #20 (reflection may suggest, never auto-apply). The
   skill author is the writer; the framework reviewer is the
   deployer; collapsing them removes the trust boundary.

**Skill-creator obligations (per `docs/cowork_skill_creator_prompt.md`):**

- The skill-creator (the Co-Work skill that emits SAI skills) MUST
  ship with an explicit catalog of existing primitives a new skill
  can use.
- When the operator describes a workflow that needs a
  not-yet-existing primitive, the skill-creator MUST refuse to
  emit the skill and instead emit a framework design doc stub +
  point the operator at this principle.
- The skill-creator MUST NOT generate Python module code that
  lives outside the skill's directory. Skill code lives ONLY at
  `~/Lutz_Dev/SAI/skills/<workflow_id>/`.

**Concrete v8 examples:**

- ✅ e1 cornell-delay-triage adds course config + TA roster +
  crisis pattern data; uses existing canonical loaders.
- ✅ Future e2 (RAG advice draft) — IF RAG primitive ships first
  in `app/canonical/rag.py` with its own design doc + tests, e2
  can compose it.
- ❌ e2 inlining a vector store + embedding call inside its
  `runner.py` — refused. Send back to "build the RAG primitive
  first."

#### 33b. Co-Work designs skills, Claude Code executes them

Skills are designed in **Co-Work** through back-and-forth with the
operator until they are executable. Co-Work is the DESIGNER.

**Claude Code (the SAI executor) takes the skill as-is and runs
it.** Claude Code is the EXECUTOR + the canonical-eval holder.
Claude Code does NOT redesign the skill, restructure its cascade,
add tiers, remove tiers, or otherwise change the WORKFLOW DESIGN.

**What Claude Code DOES do (execution layer):**
- Wires the skill's tier handlers to real LLM Providers
- Runs the cascade per the manifest
- Holds the canonical EVAL datasets (the data, not the design)
- Surfaces operator-visible signals (audit log, sai-health, sai-cost)
- Enforces the framework's eval-first + observation-first +
  security-first guardrails

**What Claude Code does NOT do:**
- Add new cascade tiers to a Co-Work skill (e.g. inserting a
  `canonical_lookup` rules tier between `rules` and `cloud_llm`
  because "deterministic is safer" — that's a DESIGN decision
  that belongs in Co-Work)
- Remove cascade tiers from a Co-Work skill
- Change the LLM-vs-rules tier balance
- Rewrite the skill's runner logic beyond the dependency-injection
  + handler-registration boilerplate the framework requires

**If the operator wants the skill changed**, the change goes back
to Co-Work. Operator iterates the skill design there until it's
executable, then hands the new version back to Claude Code. This
keeps Co-Work as the source of truth for skill design and prevents
drift from "design in chat → execute in code → re-design in code"
loops.

**The core rules** (eval-first / observation-first / security-first
+ Co-Work-designs / Claude-Code-executes) reinforce each other:
- Eval-first → the operator can SEE what the skill does without
  trusting Claude Code's redesign
- Observation-first → the audit log shows execution faithfully
- Security-first → safety guards (sender allowlist, crisis
  hard-stop, content sanitization) ARE legitimate execution-layer
  additions because they're framework-universal, not skill-design
- Co-Work-designs / Claude-Code-executes → Claude Code stays in
  its lane

**Concrete v8 lesson (2026-05-04):** the e1 cornell-delay-triage
skill from Co-Work originally had `cascade: [rules, cloud_llm,
human]`. During the "principles audit" cycle Claude Code added a
`canonical_lookup` rules tier that required deterministic
course-identifier matching before the LLM could see the email.
This was a DESIGN change Claude Code shouldn't have made. It
over-constrained the cascade (real student mail rarely says
"BANA6070" verbatim) and the operator caught the bug live. The
fix: remove the added tier, return to Co-Work's original
design + the framework's universal safety guards.

The line: framework safety guards (input_guards, second-opinion
gate) are EXECUTION layer because they apply to ALL skills.
Per-skill cascade design (rules vs LLM tier balance, course
inference logic) is DESIGN layer and belongs in Co-Work.

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
2. **Read `MIGRATION-PRINCIPLES.md`** (private overlay) for the current
   cycle's snapshot, priorities, and active task-migration queue.
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
