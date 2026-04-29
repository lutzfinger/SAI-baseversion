# Handoff #1 — overlay merge tooling

This handoff lands the overlay merge tool described in Section 3 of
`SAI-PLAN.md` ("How they merge at runtime"). It is the foundation for
Phase 1 (the hash-verifying loader) and Phase 3 (the public/private split).

## What's in this handoff

| Path | Purpose |
| --- | --- |
| `app/runtime/overlay.py` | Merge logic, manifest writer, lightweight verify, CLI |
| `app/runtime/__init__.py` | Package marker |
| `tests/runtime/test_overlay.py` | 22 contract tests + 3 verify smoke tests |
| `tests/runtime/fixtures/demo/` | Minimal public + private trees for the manual demo below |
| `pyproject.toml` | Registers `app.runtime` package and `sai-overlay` console script |

## What the merge tool guarantees

- **File-level override only.** Private files replace public files at the
  same relpath. No per-key YAML merging. (Principle 9 in the plan.)
- **Manifest with SHA-256 of every file.** Written to
  `<out>/.sai-overlay-manifest.json`. The Phase 1 loader reads this to
  detect tampering.
- **Source provenance per file.** Each manifest entry records whether the
  file came from public or private.
- **Type-conflict detection.** If the same relpath is a directory on one
  side and a file on the other, the merge refuses with `TypeConflictError`.
- **Skip rules** for VCS / build noise: `.git/`, `__pycache__/`, `.venv/`,
  `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `node_modules/`,
  `*.pyc`, `*.pyo`, `.DS_Store`. The manifest filename itself is also
  skipped from input scans.
- **`copy` (default) vs `symlink` mode.** Symlink mode is faster for dev
  iteration; Phase 1's loader will refuse symlink-mode manifests in
  strict verification mode (`UnverifiableModeError`).

## Running the tests

```sh
cd ~/Lutz_Dev/SAI-baseversion
make install                    # editable + dev extras (also re-run after any pyproject.toml change)
pytest tests/runtime/test_overlay.py -v
```

Expected: 25 tests pass (22 contract tests numbered in comments + 3
verify smoke tests).

> **Why `make install` matters even on an existing venv.** Editable installs
> cache `[project.scripts]` entries at install time. When `pyproject.toml`
> changes (e.g., the `sai-overlay` console script was added), the change
> doesn't take effect until you re-run `pip install -e .`. `make install`
> is idempotent and cheap — run it whenever `pyproject.toml` changes or
> when a shipped console script isn't on PATH.

## Manual demo

The plan's checkpoint is "merge the included fixtures, verify, see
`shadowed_count: 1`."

```sh
cd ~/Lutz_Dev/SAI-baseversion
pip install -e .                # makes the sai-overlay command available

sai-overlay merge \
  --public  tests/runtime/fixtures/demo/public \
  --private tests/runtime/fixtures/demo/private \
  --out     /tmp/sai-runtime-demo \
  --clean
```

Expected output:

```
merged 4 files -> /tmp/sai-runtime-demo
  mode: copy
  shadowed_count: 1
    private overrides public: workflows/_examples/hello.yaml
```

Verify the merged tree:

```sh
sai-overlay verify --runtime /tmp/sai-runtime-demo
```

Expected: `verify ok: /tmp/sai-runtime-demo`.

Tamper with a file and re-verify to see the failure path:

```sh
echo "tampered" > /tmp/sai-runtime-demo/workflows/_examples/hello.yaml
sai-overlay verify --runtime /tmp/sai-runtime-demo
# verify FAILED: 1 problem(s)
#   hash mismatch: workflows/_examples/hello.yaml
# (exit code 1)
```

## CLI reference

```
sai-overlay merge  --public PATH --private PATH --out PATH
                   [--mode {copy,symlink}] [--clean]
sai-overlay verify --runtime PATH
```

Exit codes:

- `0` — success
- `1` — verification problems found
- `2` — bad input (missing path, conflicting flags, type conflict, etc.)

## Manifest format (`.sai-overlay-manifest.json`)

```json
{
  "schema_version": 1,
  "mode": "copy",
  "created_at": "2026-04-27T20:30:00Z",
  "public_root": "/abs/path/to/public",
  "private_root": "/abs/path/to/private",
  "shadowed_count": 1,
  "shadowed_files": ["workflows/_examples/hello.yaml"],
  "files": {
    "workflows/_examples/hello.yaml": {
      "sha256": "…",
      "source": "private",
      "size_bytes": 137
    },
    ...
  }
}
```

## What this handoff does NOT do

These are deliberately deferred to the phases that own them:

- **Phase 1** — the runtime hash-verifying loader (`HashMismatchError`,
  `UnregisteredFileError`, `MissingFileError`, `UnverifiableModeError`,
  `SAI_OVERLAY_VERIFY` env var, `sai verify` audit logging). The
  lightweight `verify()` function in this handoff is a smoke checker
  only, used for the manual demo and three tests.
- **Phase 2** — the boundary linter that prevents personal data from
  leaking into the public repo.
- **Phase 3** — actually splitting `~/Lutz_Dev/SAI/` into public + private.
- **Phase 4** — the `sai-deploy` skill and `/sai-checkin` slash command
  that compute `version_hash` for individual workflow YAMLs.
- **Reflection / policy interfaces** (Section 6.1, 6.2) — we keep the
  merge tool narrow so a future Cedar-based evaluator or AGT adapter can
  slot in elsewhere without touching the overlay.

## Known limitations

- `--mode symlink` writes hashes to the manifest at merge time, but those
  hashes are advisory: the symlink target can change without the manifest
  knowing. Phase 1 enforces this by refusing symlink manifests in strict
  mode. Treat symlink mode as a dev-only convenience.
- Permission bits beyond what `shutil.copy2` preserves are not tracked.
  If a workflow file's executable bit ever becomes load-bearing we will
  add it to the manifest.
- Empty source directories are not represented in the output. Only
  regular files are merged.
