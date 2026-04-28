# Phase 0 — done

Goal of this phase (per `SAI-PLAN.md` Section 5, Phase 0): verify that
handoff #1's overlay merge tooling actually landed in the public repo;
if not, rebuild it; do not move on until it works end-to-end.

## What I did

### 0.1 — verified state

Walked both repos and confirmed handoff #1 was **not present anywhere**:
no `app/runtime/overlay.py`, no `tests/runtime/test_overlay.py`, no
`HANDOFF.md`, no overlay-related scripts, no `sai-overlay` console
script in either `pyproject.toml`, and zero git commits on any branch
mentioning "overlay" or "handoff" in either `SAI` (private) or
`SAI-baseversion` (public).

### 0.2 — rebuilt handoff #1 in `SAI-baseversion`

Per Lutz's call (Option A: build in baseversion, not a fresh
SAI-public scaffold).

| Path | Lines | Purpose |
| --- | --- | --- |
| [app/runtime/overlay.py](app/runtime/overlay.py) | ~290 | Merge logic, manifest writer, lightweight verify, CLI |
| [app/runtime/__init__.py](app/runtime/__init__.py) | 1 | Package marker |
| [tests/runtime/test_overlay.py](tests/runtime/test_overlay.py) | ~290 | 22 numbered contract tests + 3 verify smoke tests = 25 total |
| [tests/runtime/fixtures/demo/](tests/runtime/fixtures/demo/) | 5 files + README | Demo trees that yield `shadowed_count: 1` |
| [HANDOFF.md](HANDOFF.md) | — | Manual demo, CLI reference, manifest format, what's deferred |
| [pyproject.toml](pyproject.toml) | +2 lines | `app.runtime` package + `sai-overlay` console script |

### 0.3 — end-to-end checks

```
pytest tests/runtime/test_overlay.py        25 passed in 0.09s
ruff check app/runtime tests/runtime        All checks passed
mypy app/runtime  (strict)                  Success: no issues found in 2 source files
sai-overlay merge ... --out /tmp/...        merged 4 files, shadowed_count: 1
sai-overlay verify --runtime /tmp/...       verify ok
sai-overlay verify (after tampering)        FAILED: 1 problem(s) — hash mismatch
```

## Contract surfaces this handoff guarantees

- **File-level override only.** Private replaces public at the same
  relpath. No per-key YAML merging.
- **Manifest with SHA-256 + provenance per file**, written to
  `<out>/.sai-overlay-manifest.json`. Schema version 1.
- **Type-conflict detection.** Same path as a dir on one side and a
  file on the other → `TypeConflictError`.
- **`copy` (default) vs `symlink` mode.** Phase 1 will refuse symlink
  manifests in strict mode (`UnverifiableModeError`).
- **CLI exit codes**: 0 success, 1 verification problems, 2 input errors.
- **Skip rules**: `.git/`, `__pycache__/`, `.venv/`, `.mypy_cache/`,
  `.pytest_cache/`, `.ruff_cache/`, `node_modules/`, `*.pyc`, `*.pyo`,
  `.DS_Store`, plus the manifest filename itself.

## Deviations from the plan, called out

- The plan promised "22 tests"; this delivery has **25** (the 22
  numbered contract tests in `test_overlay.py` plus 3 smoke tests
  for the lightweight `verify()`). The 22 minimum is satisfied.
- The plan describes a richer verifier with typed exceptions
  (`HashMismatchError`, `UnregisteredFileError`, `MissingFileError`,
  `UnverifiableModeError`) and `SAI_OVERLAY_VERIFY` env var. Those
  belong to **Phase 1** (the runtime loader) and are explicitly out
  of scope here. The Phase 0 `verify()` returns lists, which is
  enough for the demo and tests.

## What this handoff does NOT do (deferred to later phases)

- Phase 1 — runtime hash-verifying loader, typed exceptions, audit
  logging, `SAI_OVERLAY_VERIFY` env var.
- Phase 2 — boundary linter to keep personal data out of public.
- Phase 3 — actual repo split.
- Phase 4 — `sai-deploy` skill and `/sai-checkin` slash command,
  per-workflow `version_hash`.

## State of the repo after this phase

```
On branch main
Your branch is ahead of 'origin/main' by 1 commit.
  ↑ pre-existing commit "ask for contribution support" — not from this phase
Untracked / modified by this phase:
  pyproject.toml      (modified)
  HANDOFF.md          (new)
  app/runtime/        (new)
  tests/runtime/      (new)
  PHASE-0-DONE.md     (this file, new)
```

Nothing else in baseversion was touched.

## Next

Phase 1 — hash-verifying loader. Wired into the spot where SAI
currently loads workflow YAML; fail-closed on hash mismatch,
unregistered files, missing files, and (in strict mode) symlink
manifests. Not started.
