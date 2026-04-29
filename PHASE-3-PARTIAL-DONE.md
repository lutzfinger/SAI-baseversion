# Phase 3 — partial (3A + bulk of 3C)

Goal of this phase (per `SAI-PLAN.md` §5 Phase 3): split private SAI's
working tree into framework code (here, in SAI-baseversion) + private
overlay (in `github.com/lutzfinger/SAI`). This commit lands the additive
half: framework code and sanitized example workflows arrive in public.
The destructive half (removing framework code from private + runtime
cutover) is intentionally deferred.

## What I did

### 3A — classifier dry-run

Built [scripts/classify_repo_split.py](../SAI/scripts/classify_repo_split.py)
in private SAI. Walks the private working tree (498 files) and tags each
with one of: `framework-pure`, `framework-divergent`, `template-candidate`,
`private-data`, `already-public-match`, `binary`, `skipped-secrets`. Output
manifest: `split-classification.json`.

Final breakdown:

| Category | Count |
| --- | --- |
| framework-pure | 184 |
| private-data | 187 |
| template-candidate | 52 |
| framework-divergent | 45 |
| already-public-match | 24 |
| skipped-secrets | 7 (incl. `.env`, OAuth tokens, `.DS_Store`) |
| binary | 4 |

### 3C — additive moves into public

Built [scripts/apply_split.py](../SAI/scripts/apply_split.py): reads the
manifest, executes file-level actions, never deletes from private. Ran
once. Result on the public side:

- **140 framework files added** under `app/`, `prompts/`, `registry/`,
  `tests/`, etc.
- **52 sanitized example workflows** added under `workflows/_examples/`
  (boundary-linter-clean — `you@example.com`, `#general`, etc.)
- **3 mixed files modified**: `pyproject.toml` (langgraph + langgraph-
  checkpoint-sqlite + langchain-core/langsmith bumps),
  `boundary_check_allowlist.txt` (Phase 2 doc + boundary test entries),
  `tests/conftest.py` (collect_ignore list for new tests with
  unsatisfied deps).

Boundary linter clean: **307 files scanned, 0 violations, 6 allowlist
entries.**

### Pre-flight import-graph check (caught an entanglement)

[scripts/apply_split.py](../SAI/scripts/apply_split.py) walks every
framework-pure `.py` file and inspects its imports. **28 framework-pure
modules import from private-data modules** — moving them would break
imports on the public side. The script reclassifies those 28 as
**deferred TODOs**; they stay private until either (a) the imported
modules can also become framework-pure, or (b) the importer is
refactored to not need the private dep.

### Reverted: "adopt private" overwrites

The first apply_split run had a "framework-divergent + no personal-data
signals → adopt private" rule. That broke when public's `langsmith.py`
had functions (`flush_langsmith_tracers`, `create_langsmith_client`)
that private's version had refactored away — overwriting silently broke
public's existing `runner.py`. **All 30 file overwrites reverted via
`git checkout HEAD --`.** Heuristic removed; framework-divergent files
now require manual reconciliation.

## Test status

```
85 passed, 1 failed (flaky), 1 skipped, 12 collect-ignored
```

- **85 pass** — all of Phase 0/1/2 plus the new framework code that doesn't
  trigger the issues below.
- **1 flaky** — `test_langsmith_settings.py::test_settings_accept_standard_langsmith_env_names`
  passes alone, fails after another (still-being-tracked-down) test
  imports `app.shared.config` and triggers `load_runtime_env_best_effort()`.
  Symptom: `assert settings.langsmith_project == 'starter-traces'`
  fails because `SAI_LANGSMITH_PROJECT="SAI"` from `~/.config/sai/runtime.env`
  takes precedence via the `AliasChoices` validation_alias. **Phase 3.5
  cleanup task.**
- **12 collect-ignored** — listed in `tests/conftest.py::collect_ignore_glob`.
  Reasons: test files copied from private that need either richer test
  fixtures (`test_settings`-style) or framework-divergent file
  reconciliation (`EmailTriageComparison`, `OpenAILLMClient`, etc.).

## Untracked-but-on-disk

13 test files were copied from private into public's working tree but are
NOT staged for git. They're in `tests/conftest.py::collect_ignore_glob`
so pytest skips them. Each maps to a Phase 3.5 follow-up:

| File | Blocked by |
| --- | --- |
| `test_langsmith.py`, `test_local_cloud_learning.py`, `test_slack_joke_workflow.py` | framework-divergent: `email_models.EmailTriageComparison`, `local_llm_classifier.OpenAILLMClient` need framework/overlay split |
| `test_approvals.py`, `test_background_services.py`, `test_calendar_connector.py`, `test_fact_memory.py`, `test_prompt_hashes.py`, `test_reflection.py`, `test_replay.py` | richer `test_settings` fixture not yet in public's conftest |
| `test_runtime_env.py` | pollutes `os.environ` via `load_runtime_env_best_effort()` — needs test isolation |
| `test_gmail_taxonomy_labels.py` | private taxonomy data not loaded |
| `tests/fixtures/` directory | unused without the above tests |

## What this phase does NOT do

- **3D — backport unpushed private commits' framework parts.** The 4 unpushed
  commits in private SAI (path refactor, migration script, conftest fixes,
  log maintenance) — their framework parts haven't been backported to
  public yet. The path-config refactor in particular was done locally in
  private; public still has its old defaults.
- **3E — runtime rewire.** SAI's startup still loads from
  `/Users/lfinger/Lutz_Dev/SAI/`, not the merged `~/.sai-runtime/`. No
  overlay merge happens at runtime.
- **3F — cutover.** Private SAI hasn't had its framework code removed.
  Private continues to be the runtime; public is library-only for now.
- **3G — push to GitHub.** Two unpushed commits on public main + this
  Phase 3 partial commit will be three when committed. Deferred to user
  decision.

## State of the repo after this phase

```
SAI-baseversion (public):
  + 140 new framework files (app/, prompts/, registry/, tests/, scripts/, etc.)
  + 52 sanitized example workflows in workflows/_examples/
  + langgraph + langgraph-checkpoint-sqlite + bumped langchain/langsmith
  + collect_ignore list for 12 tests with unresolved deps
  + 13 test files staged on disk but not git-tracked (Phase 3.5)
```

## Next

The branching point for Phase 3.5+:

1. **Iterative reconciliation** — pick one framework-divergent file at a
   time. Diff private vs public. Decide: adopt-private / split-into-
   framework+overlay / drop-private-changes. Apply. Re-enable the test
   that depended on it.

2. **Resolve the 28 framework-pure-with-private-imports** — for each of
   those, either bring the imported private module to public (after
   sanitization) or refactor the importer to avoid the dep.

3. **Backport private's 4 unpushed commits' framework parts** to public.

Once 1+2+3 land, public can be runnable on its own (plan §3 acceptance
test). Then 3E (runtime rewire) and 3F (cutover) become safe.
