# Migration backlog

Modules that were copied into the public starter during the Phase 3 split
but couldn't run because their dependencies (notably `OpenAILLMClient`,
`app.graphs.email_triage`, `app.workers.classification_correction`,
`TaskApproachPlannerTool`) are private-only. Rather than ship dead code,
they were dropped from the public tree on `main` and queued here for
reshape under the AI-Stack architecture. Each entry below describes:

- The previous file location (now deleted)
- The Task it belongs to under the new architecture
- The tier shape it lands as
- The dependencies still needed
- The private-repo source you can pull from when you do the migration

## 12 modules to migrate as tier impls

| Old path | Task | Tier shape | Deps still missing |
|---|---|---|---|
| `app/tools/travel_operation.py` | `travel_operation` | CloudLLMTier | OpenAI Responses provider (✓ shipped step 9), prompt template, `BookingCandidate`/`BookingDecision` schemas |
| `app/tools/people_of_interest_research.py` | `people_research` | CloudLLMTier | OpenAI provider (✓), web-search tool wrapper |
| ~~`app/tools/safe_joke_writer.py`~~ MIGRATED 2026-05-01 | `slack_joke` | CloudLLMTier + RulesTier | shipped: `app/tasks/slack_joke.py` (private factory), `app/tasks/slack_joke_io.py` (public schema), `registry/tasks/slack_joke.yaml`, integration test |
| `app/tools/video_analysis.py` | `video_analysis` | CloudLLMTier (multimodal) | OpenAI provider (✓), video → frame extractor (private-only) |
| `app/tools/reply_planning.py` | `reply_planning` | CloudLLMTier | OpenAI provider (✓) |
| `app/workers/email_triage.py` | `email_classification` | runner caller | TaskFactory + the existing email_classification.yaml (✓ shipped step 8) |
| `app/workers/reply_planning.py` | `reply_planning` | runner caller | task config + tier wiring |
| `app/workers/task_assistant.py` | `task_assistant` | CloudLLMTier + RulesTier | `TaskApproachPlannerTool` reshape + task config |
| `app/workers/people_of_interest.py` | `people_research` | runner caller | task config + tier wiring |
| `app/workers/classification_alignment_intake.py` | `email_classification` (eval pipeline) | reconciler addon | needs `classification_correction` module from private |
| ~~`app/workers/slack_joke_models.py`~~ MIGRATED 2026-05-01 | `slack_joke` | runner caller | superseded by `app/tasks/slack_joke_io.py` (public schemas) |
| `app/graphs/reply_planning.py` | `reply_planning` | LangGraph orchestrator | merges into the runner; may not need separate graph |

Plus: `scripts/analyze_video.py` (CLI front-end for video_analysis) — restore when video_analysis lands.

## Per-task migration template

For each task above, one focused session:

1. Read the private-repo originals at `$SAI_PRIVATE/app/...`
2. Identify framework code vs. data — Lutz's prompts, channel names,
   and operator-specific content stay in private; the LLM call shape,
   schema, retry logic, etc. come to public.
3. Write `registry/tasks/<task_id>.yaml` (TaskConfig schema; copy
   `email_classification.yaml` as a starting point).
4. Write tier implementations in public — usually a thin adapter that
   takes a Provider + prompt template + schema and produces a
   `LocalLLMTier` or `CloudLLMTier`. For RulesTier, the existing
   `keyword_classifier`-style code in private gets reshaped.
5. Add public integration test in `tests/integration/`.
6. In private overlay: write the TaskFactory that loads the YAML +
   instantiates tiers with real OAuth tokens + API keys + prompts.
7. Smoke test against real data behind a launchd job; observe
   EvalRecord output.

The end state: every entry above has been re-shipped as `app/tasks/<task_id>.py`
plus its tier impls. The shapes are repeatable enough that after the first
2–3 task migrations, the rest go quickly.

## Phase 3.5 (1): 46 framework-divergent files

These are files where the public starter and private overlay both have a
copy with material differences (not just paths or channel names). The
classifier flagged them but per-file resolution requires judgment that's
specific to each file's role.

**Defer policy:** handle as part of per-task migrations. Most divergent
files belong to a specific task — when that task migrates, the divergent
file gets resolved (private wins for data, public wins for framework
shape). Forcing all 46 files through a single sweep produces a lot of
shallow per-file diffs that don't fit the task-migration cadence.

If you want a one-shot reconciliation later, the classifier output lives
at `$SAI_PRIVATE/split-classification.json` and the apply-script
machinery is at `$SAI_PRIVATE/scripts/apply_split.py`.

## Phase 3.5 (5): 12 collect-ignored tests

Listed in `tests/conftest.py` `collect_ignore_glob`. They lost their
imports when the modules above were dropped:

- `test_langsmith.py`, `test_local_cloud_learning.py`,
  `test_slack_joke_workflow.py` → tests for dropped framework-divergent
  modules; re-enable as part of the corresponding task migration.
- `test_approvals.py`, `test_background_services.py`,
  `test_calendar_connector.py`, `test_fact_memory.py`,
  `test_prompt_hashes.py`, `test_reflection.py`, `test_replay.py` →
  need a richer `test_settings` fixture in `tests/conftest.py`. Each
  task migration that needs them can extend the fixture.
- `test_runtime_env.py` → pollutes `os.environ` via
  `load_runtime_env_best_effort()`; needs proper isolation. Independent
  of task migration.
- `test_gmail_taxonomy_labels.py` → returns empty taxonomy without
  private's data. Tied to the email_classification task migration.

These delete cleanly along with the modules above. The
`collect_ignore_glob` still references them, but that's harmless until
the files actually get re-added. The next maintenance pass can clean up
the glob entries.
