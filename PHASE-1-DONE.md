# Phase 1 — done

Goal of this phase (per `SAI-PLAN.md` Section 5, Phase 1): make the trust
property real. Phase 0 wrote SHA-256 of every merged file into
`.sai-overlay-manifest.json`; Phase 1 adds the loader that **uses** those
hashes — fail-closed on tampering, unregistered files, missing files, and
(in strict mode) symlink-mode manifests.

## What I did

### 1.1 — typed manifest + verifier

| Path | Lines | Purpose |
| --- | --- | --- |
| [app/runtime/manifest.py](app/runtime/manifest.py) | ~95 | Typed dataclass `Manifest` + `FileEntry`; raises `ManifestNotFoundError` / `ManifestCorruptError` |
| [app/runtime/verify.py](app/runtime/verify.py) | ~225 | `Verifier` class, typed exceptions, env-var mode resolution, `build_verifier_for_runtime` |
| [app/runtime/verify_cli.py](app/runtime/verify_cli.py) | ~70 | `sai-verify --runtime PATH` console script |

Exception hierarchy (all subclass `OverlayVerifyError`):

- `HashMismatchError` — file content sha256 differs from manifest
- `UnregisteredFileError` — file on disk but not in manifest
- `MissingFileError` — manifest references file but it's missing
- `UnverifiableModeError` — symlink-mode manifest in strict (or warn)

### 1.2 — env var

`SAI_OVERLAY_VERIFY` resolves to `strict` (default), `warn`, or `off`.
Invalid values raise. Case-insensitive.

### 1.3 — `verify_all()` + CLI

`Verifier.verify_all()` walks the runtime tree once, reports every problem
in one pass (does not stop at first). Wired to `sai-verify --runtime PATH`
console script.

### 1.4 — tests

[tests/runtime/test_verify.py](tests/runtime/test_verify.py) — **22 tests, all pass**:

- happy path (clean strict, per-file verify, verify_all)
- HashMismatchError on tamper
- UnregisteredFileError on stray file
- MissingFileError on deleted file
- UnverifiableModeError in strict for symlink manifest
- symlink + warn logs but does not raise
- off mode skips everything (even tampered files)
- verify_all aggregates multiple problem types in one pass
- failure callback receives structured records (Phase 1.5)
- callback exceptions don't mask the original error
- ManifestNotFoundError / ManifestCorruptError surface cleanly
- env var resolution (default, case-insensitive, invalid)
- `build_verifier_for_runtime` returns None for unset/no-manifest/off
- `WorkflowStore` integration: tampered YAML rejected before parse
- backward compat: store without verifier behaves exactly as before

### 1.5 — audit log integration

`Verifier(on_failure=callback)` invokes the callback on every failure with a
structured `VerificationFailureRecord` (relpath, error_type, expected_sha256,
actual_sha256, mode, manifest_mode, timestamp).

[app/control_plane/runner.py](app/control_plane/runner.py) wires this to
`AuditLogger.append_event` so every verification failure becomes one audit
row with `event_type="overlay_verify_failure"`. Callback exceptions are
caught and logged — they cannot mask the original verification error.

### Loader integration

[app/control_plane/loaders.py](app/control_plane/loaders.py) — `PromptStore`,
`PolicyStore`, `WorkflowStore`, `PromptLockStore` each accept an optional
`verifier: Verifier | None` parameter. When set, every `load()` calls
`verifier.verify(path)` before reading the file. When None, behavior is
unchanged (backward compat).

[app/control_plane/runner.py](app/control_plane/runner.py) constructs the
verifier from `settings.overlay_runtime_root` (a new field, default `None`)
and passes it to all four stores. When the runtime root isn't set (e.g.
running directly out of the public starter repo), no verifier is created and
loaders behave as before.

### pyproject

Added `sai-verify = "app.runtime.verify_cli:cli"` to `[project.scripts]`.
Run `make install` (or `pip install -e .`) to register the new console
script.

## Contract surfaces

- **Manifest schema** (read-only, written by `sai-overlay merge`): unchanged
  from Phase 0.
- **Verifier API**:
  - `Verifier(runtime_root, *, manifest=None, mode=None, on_failure=None)`
  - `verify(path: Path) -> None` — raises in strict, warns in warn, no-op in off
  - `verify_all() -> list[OverlayVerifyError]` — never raises; returns problem list
  - `relpath_for(path: Path) -> str`
- **Settings.overlay_runtime_root** (new field, optional `Path`).
- **CLI**: `sai-verify --runtime PATH [--mode strict|warn|off]`. Exit codes:
  0 clean, 1 problems found, 2 input error.

## End-to-end checks

```
pytest tests/runtime/test_verify.py     22 passed
pytest tests/runtime/                   47 passed (Phase 0 + Phase 1)
pytest                                  54 passed
sai-verify --runtime /tmp/.../runtime   verify ok (mode=strict, files=4)
sai-verify (after tampering)            FAILED: HashMismatchError, exit 1
```

## Deviations from the plan

- Plan section 1.5 says "Audit-log every verification failure: relpath,
  expected hash, actual hash, mode, timestamp." I added one extra field
  (`manifest_mode`) so the audit row tells you whether the manifest was
  copy-mode or symlink-mode at failure time — useful when triaging.

## What this phase does NOT do

Deferred to later phases:

- **Phase 2** — boundary linter that prevents personal data from leaking into
  the public repo.
- **Phase 3** — the actual repo split (settings.overlay_runtime_root only
  becomes load-bearing when private workflows live in a separate tree).
- **Phase 4** — `sai-deploy` skill and `/sai-checkin` slash command that
  compute per-workflow `version_hash` (a separate property from the overlay
  manifest hash).
- **Section 6.3** — Asqav-style hash-chain audit. The single audit-log
  chokepoint at `AuditLogger.append_event` is the right place to add this
  later; nothing in Phase 1 prevents it.

## State of the repo after this phase

```
On branch main
Untracked / modified by this phase:
  app/runtime/manifest.py        (new)
  app/runtime/verify.py          (new)
  app/runtime/verify_cli.py      (new)
  app/control_plane/loaders.py   (modified: optional verifier per store)
  app/control_plane/runner.py    (modified: build verifier + pass to stores + audit callback)
  app/shared/config.py           (modified: added overlay_runtime_root field)
  pyproject.toml                 (modified: added sai-verify console script)
  tests/runtime/test_verify.py   (new, 22 tests)
  PHASE-1-DONE.md                (this file, new)
```

## Next

Phase 2 — boundary linter. Prevents personal data (email addresses,
`lutzfinger`, `/Users/`, real Slack channel names, `op://`, etc.) from
landing in the public repo. Pre-commit hook + GitHub Actions guardrail.
