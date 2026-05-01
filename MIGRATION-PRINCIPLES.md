# SAI migration: cycle plan + principles

**Date stamp:** 2026-04-30
**Replaces:** `~/Downloads/SAI-PLAN (1).md` (the original handoff plan, 2026-04-27)
**Read alongside:** `PRINCIPLES.md` (the durable rules)

This is the **current migration cycle**. It captures where we are, what's
shipped, what's active, and what's next — all the time-bound stuff that
shouldn't drift into the durable principles doc. Replace it (with a new
date) when the next cycle starts.

If you (or a future Claude session) start work on SAI:

1. Read `PRINCIPLES.md` first.
2. Read this file second. The "Where we are" snapshot below tells you
   what's done; the "Active priorities" tell you what's next.
3. Don't start a new phase or task before reading both.

---

## Where we are (snapshot — 2026-04-30)

### Architecture: shipped

The eval-centric AI Stack is complete and operable:

| Layer | Status | Public path |
|---|---|---|
| Eval primitives (EvalRecord, Preference, Ask, storage) | shipped | `app/eval/` |
| Provider abstraction (4 vendors: OpenAI, Anthropic, Gemini, Ollama) | shipped | `app/llm/` |
| Tier protocol + 5 tier implementations | shipped | `app/runtime/ai_stack/` |
| Cascade runner (sequential, early-stop) | shipped | `app/runtime/ai_stack/runner.py` |
| Slack Ask UI (block-kit, email-aware formatting) | shipped | `app/connectors/slack_ask_ui.py` |
| Reality reconcilers (Slack reply, Gmail label) | shipped | `app/eval/reconcilers/` |
| Ask orchestrator (coverage + budget + disagreement) | shipped | `app/eval/orchestrator.py` |
| Reply parser factories (option-matching + permissive) | shipped | `app/eval/reply_parsers.py` |
| Preference refiner (Loop B) | shipped | `app/eval/preference_refiner.py` |
| Hash-verifying loader | shipped | `app/runtime/verify.py` |
| Overlay merge tool (`sai-overlay merge`) | shipped + verified | `app/runtime/overlay.py` |
| Boundary linter | shipped | `scripts/boundary_check.py` |

Public test suite: 262 pass / 1 preexisting skip. Boundary + ruff clean.

### Cleanup: shipped

- Phase 3.5 (2b): 12 broken modules + 3 orphaned tests dropped from public
  per `MIGRATION-BACKLOG.md`. Each entry there describes target tier shape
  for re-introduction.
- Phase 3.5 (5): orphaned tests purged from `collect_ignore_glob`.
- Phase 3E: `sai-overlay merge` tool verified end-to-end against the real
  public + private trees (689 files, 13MB merged, manifest clean).
- Phase 3F: cutover runbook + helper script ready (`scripts/sai_cutover.sh`,
  `PHASE-3F-CUTOVER.md`).

### Phase 3.5 (1) — explicit defer

The 46 framework-divergent files are deferred to per-task migration. The
overlay handles them at runtime (private wins). Per-file reconciliation
happens as task migrations naturally surface divergent files.

---

## Active priorities

### 1. Email classifier task (Sub 1-1, Sub 1-2) — IN PROGRESS

This is the first task migration off the old `email_triage.py` graph onto
the new AI-Stack runtime. The proof that the architecture works on real
data.

Status:
- Sub 1-1 (email_models overlay verified): ✓
- Sub 1-2a (private TaskFactory at `app/tasks/email_classification.py`): ✓
- Sub 1-2b (backtest harness at `scripts/backtest_email_classifier.py`): ✓
- Sub 1-2c (runbook at `docs/EMAIL-CLASSIFIER-MIGRATION.md`): ✓
- L1-aware reply parser (private wires public factory): ✓
- Slack Ask UI: email-aware formatting + confirmation/clarification: ✓
- `scripts/poll_eval_replies.py` polling daemon: ✓
- Smoke 2-4 with real Gmail + OpenAI + Slack: pending operator
- Side-by-side period (≥1 week observation): pending
- Phase 3F cutover (interactive plist flip): pending

### 2. Phase 4 — deploy skill + `/sai-checkin` slash command

Foundation for the skill ecosystem. Not started.

Scope:
- A `~/.claude/skills/sai-deploy/` skill that takes a workflow file and
  decides which repo to write to.
- A `/sai-checkin` slash command that hashes, lints, tests, and commits.
- The runtime computes hashes the same way and refuses unmatched ones.
- Test loop: design in cowork → write to private → check-in → restart →
  verify load.

### 3. Phase 5 — `sai-run` bridge skill

The closed-loop bridge between Cowork and SAI. Not started.

Scope:
- A `~/.claude/skills/sai-run/SKILL.md` that lets Cowork invoke SAI
  workflows by name via `127.0.0.1:8000/api/workflows/<name>/run`.
- Handles `awaiting_approval` responses correctly.
- The control plane needs a `/api/workflows` endpoint listing available
  workflows.

### 4. Use-case (b) LinkedIn end-to-end

Cross-cutting: a LinkedIn task migration in SAI + a Claude skill that
bridges via `sai-run`. Depends on Phase 5.

### 5. Use-case (c) RAG skill (or any first Claude skill)

A real Claude skill exercising the Phase 4 + Phase 5 stack end-to-end.
Probably the RAG indexing skill from the original 16-skill set, since
it doesn't have external side effects (Group C).

---

## Migration backlog

`MIGRATION-BACKLOG.md` lists 12 task migrations queued (the 12 broken
modules dropped during Phase 3.5 cleanup). Each entry has:

- Original module location
- Target Task name
- Target tier shape (CloudLLMTier / runner caller / etc.)
- Missing dependencies needed before it can be reshipped

The migration backlog is worked down per task per session, in priority
order set by the operator. The original SAI-PLAN's "Group A skills"
(`social-post`, `linkedin-connection-triage`, etc.) overlap with the
backlog; treat the backlog as the canonical queue.

---

## Migration-cycle principles

These complement `PRINCIPLES.md` for the duration of this cycle. They
expire when the cycle does.

### Per-task migrations, never big-bang

Each task gets its own session: TaskConfig YAML + tier impls + tests
+ private TaskFactory + integration test + smoke + cutover. Never
batch multiple tasks. Each is a 1–2 session unit.

### Approval-required default for first deploy

When migrating a task that takes external action (Group A: post,
send, modify), default policy = `approval-required`. Downgrade to
`allow` only after observing the workflow run cleanly in approval
mode for ≥1 week.

### Backtest before cutover

Side-by-side run of new architecture against old for ≥1 week.
Compare outputs in EvalRecords. Flip the launchd job (or whatever
production trigger) only when comfortable.

### Rollback in <60 seconds

Every migration's rollback path = one config edit + one launchd reload
(or equivalent). If the rollback takes longer, the cutover isn't ready.

### Drop, don't delete (during migration)

When a new task supersedes an old worker/graph, mark deprecated with
a comment; don't `git rm`. Old code stays available for ≥1 month
before pruning. Easy rollback, audit-friendly history.

### Phase reports

Every phase or major migration produces a `PHASE-N-DONE.md` (for
phases) or `<task>-MIGRATION.md` (for tasks). Lists what landed,
what's deferred, the smoke checklist for the operator. Commit it
alongside the code.

---

## What remains from the original SAI-PLAN

The original `SAI-PLAN (1).md` (2026-04-27) had 7 phases (0–7) plus
future capabilities. Reconciling that plan against current state:

| Original phase | Status |
|---|---|
| Phase 0: Verify foundation (overlay merge tooling) | shipped (commit `381bfe9`) |
| Phase 1: Hash-verifying loader | shipped (commit `eef8a21`) |
| Phase 2: Boundary linter | shipped (commit `5e98b24`) |
| Phase 3: Repo split (private + public) | shipped partial + Phase 3.5 cleanup |
| Phase 3E: Runtime rewire (overlay merge tool) | shipped (commit `381bfe9`, refined `4ce6dd9`) |
| Phase 3F: Cutover (interactive plist flip) | runbook ready, **operator-pending** |
| Phase 4: Deploy skill + `/sai-checkin` | **not started** |
| Phase 5: `sai-run` bridge skill | **not started** |
| Phase 6: First migration (`social-post`) | **superseded by email_classification first** (active) |
| Phase 7: Remaining Group A migrations | **queued** in MIGRATION-BACKLOG.md |
| Future: Cedar policy, AGT integration, hash-chain audit | deferred |

The plan's intent is preserved: the AI-Stack architecture replaces
"workflows + policies + workers" with "Task config + tier impls +
runner," but the trust properties (gate before side effects, append-
only audit, hash-verified loading, fail closed) all hold.

---

## Per-task migration template

For each task migration session:

1. **Read the private original.**
   `~/Lutz_Dev/SAI/app/...` — read the existing worker/graph/tool to
   understand current behavior.

2. **Identify framework vs data.**
   What's universal (call shape, retry logic, schema validation)? What's
   the operator's specific values (prompts, taxonomy, channel names)?

3. **Write `registry/tasks/<task_id>.yaml` (public).**
   Copy `email_classification.yaml` as a starting point. Set
   `active_tier_id`, `escalation_policy`, `reality_observation_window_days`,
   `graduation_thresholds`. Don't include real channel names or
   operator-specific values.

4. **Write tier impls (public framework).**
   Usually thin adapters: `LocalLLMTier(provider=..., prompt_renderer=...)`
   or `RulesTier(rule_fn=...)`. The shape is the framework's; the
   provider/prompt/rule_fn parameters come from operator code.

5. **Write integration test (public).**
   `tests/integration/test_<task_id>_e2e.py` — narrative test using
   scripted tiers + stub clients. Verify cascade, escalation, reality
   reconciliation paths.

6. **Write the private TaskFactory.**
   `app/tasks/<task_id>.py` in the operator's overlay — wires real
   provider keys, real prompts, real OAuth tokens, real Slack channels
   into the public factories.

7. **Backtest with smoke harness.**
   `scripts/backtest_<task_id>.py` (private) — pulls a sample of real
   inputs, runs through the new task, prints results, optionally posts
   Slack asks for ground-truth corrections.

8. **Side-by-side observation (≥1 week).**
   Old worker keeps running on its existing schedule. New task runs
   alongside via the backtest harness or a parallel scheduled job.
   Compare outputs in EvalRecords.

9. **Cutover (interactive).**
   `scripts/sai_cutover.sh --switch` + plist edit + reload. Document
   what changed in `<task>-MIGRATION.md`.

10. **Document in MIGRATION-BACKLOG.**
    Mark the entry as completed with the cutover commit hash. Move on
    to the next.

---

## Cutover principles

From `PHASE-3F-CUTOVER.md`, generalized:

1. Old code stays in place during cutover. Rollback flips back to it.
2. Plist edits are user-confirmed (never auto-apply).
3. `--build` produces the merged tree without touching launchd.
4. `--switch` unloads launchd + builds + asks the operator to edit the
   plist; doesn't reload.
5. `--reload` is the explicit "go" step.
6. `--rollback` unloads + asks operator to flip plist back.
7. Backups: copy the launchd plist before any edit.
8. The merge tree is read-only at runtime. SAI never writes back to it.
9. Re-merging after public/private updates is `--build` again
   (idempotent, manifest re-hashes).

---

## Glossary (migration-cycle specific)

- **Sub 1-N** — sub-step of Milestone 1 (email classifier task migration).
- **Smoke 2/3/4** — the four progressive smoke tests in
  `EMAIL-CLASSIFIER-MIGRATION.md`. (Smoke 1 is "imports cleanly.")
- **Cutover** — flipping the production runtime path from
  `~/Lutz_Dev/SAI/` to `~/.sai-runtime/`.
- **Phase 3F** — the cutover phase from the original plan.
- **Group A / B / C skills** — the 16 original Claude account-level
  skills, classified by side-effect risk in the original plan. Group A
  ports to SAI workflows; Group B stays as Claude Code skills with
  drafts only; Group C is system ops.
- **MIGRATION-BACKLOG** — `MIGRATION-BACKLOG.md`, the 12 dropped
  modules awaiting reshipment as tier impls in their respective tasks.

---

*When this cycle ends — when Phase 3F cutover lands, the email task is
running on the new architecture, the first Claude skill exercises
sai-run, and the LinkedIn end-to-end is done — replace this file with
a new dated migration plan that captures the next cycle's priorities.
Don't accumulate stale state.*
