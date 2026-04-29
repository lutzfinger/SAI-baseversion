# Phase 2 — done

Goal of this phase (per `SAI-PLAN.md` Section 5, Phase 2): a boundary
linter that prevents personal data from leaking into the public repo
before the first real commit. Pre-commit hook + GitHub Actions check.

## What I did

### 2.1 — `scripts/boundary_check.py`

Walks every tracked file (via `git ls-files`, falling back to a directory
walk when run outside git) and fails on any of these patterns:

| Rule | What it catches | Placeholders allowed |
| --- | --- | --- |
| `email-non-placeholder` | `user@<domain>` with domain other than `example.com` / `example.org` / `localhost` / `test` | the four placeholder domains |
| `personal-string` | `lutzfinger`, `lfinger`, `Lutz_Dev` (case-insensitive, word-bounded) | — |
| `users-path` | `/Users/<name>/...` | `/Users/example/...` |
| `slack-channel-non-placeholder` | `#channel-name` (with CSS hex-color exclusion: `#fff`, `#444`, `#d0d7de`, `#abcdef12` are NOT Slack channels) | `#general`, `#example`, `#test-channel` |
| `phone-number` | `(415) 555-1234` / `415-555-1234` / `+1.415.555.1234` etc. | `5555555555`, `1234567890`, `0000000000`, and tokens with ≤2 unique digits |
| `secret-scheme-reference` | `op://...`, `keychain://...` | — |

Binary files (`.png`, `.pdf`, `.sqlite`, `.pyc`, etc.) are skipped via
extension list + NUL-byte sniff.

Exit codes:
- `0` clean
- `1` violations found
- `2` bad input (root doesn't exist, etc.)

### 2.2 — explicit allowlist with justification

[boundary_check_allowlist.txt](boundary_check_allowlist.txt) — one path
per line, every entry MUST be followed by a comment explaining why. Today:

| Path | Reason |
| --- | --- |
| `HANDOFF.md` | Setup doc with `cd ~/Lutz_Dev/...` install instructions |
| `PHASE-0-DONE.md` | Phase report describing where Phase 0 artifacts landed |
| `PHASE-1-DONE.md` | Phase report references the rule patterns themselves inside backticks |
| `app/shared/runtime_env.py` | Framework code that **parses** `keychain://` references — must mention the scheme name to do its job |

### 2.3 — pre-commit + GitHub Actions

- [.pre-commit-config.yaml](.pre-commit-config.yaml): hook runs the linter
  on every staged file via `--paths`. Install once with `pre-commit install`.
- [.github/workflows/boundary.yml](.github/workflows/boundary.yml): runs
  the linter on every push and PR to `main`. PRs that fail cannot merge.

### 2.4 — clean baseline established

```
boundary check ok: 108 files scanned, 0 violations (allowlist entries: 4)
```

The CSS hex-color false positives that the first run flagged
(`#d0d7de`, `#444` in `app/connectors/gmail_send.py`) are now correctly
excluded — pure-hex tokens of length 3, 4, 6, or 8 are colors, not Slack
channels. Documented in the linter as the only structural exception.

## Tests

[tests/runtime/test_boundary_check.py](tests/runtime/test_boundary_check.py)
— **19 tests, all pass**:

- per-rule scan: clean lines, placeholder vs real domain, personal strings,
  `/Users/` paths, Slack channels (with placeholder + CSS hex-color
  exclusions), phone numbers (with placeholder digits exclusion), `op://` /
  `keychain://` schemes
- aggregation: a single line containing email + path + personal string flags
  three rule types
- end-to-end via `main()`: clean repo returns 0, dirty repo returns 1,
  allowlist file exempts a flagged path, `--paths` arg scans only the listed
  files (pre-commit shape), invalid root returns 2, `--list-rules` exits 0
  with rule descriptions

Total SAI-baseversion test count after Phase 2: **73 passed**.

## Contract surfaces

- Linter is invoked as `python scripts/boundary_check.py [--root PATH]
  [--paths FILE...] [--list-rules]`. The default `--root` is the repo root
  (the parent of `scripts/`).
- Allowlist file is `boundary_check_allowlist.txt` at the repo root. Lines
  starting with `#` and blank lines are ignored. Each path entry should be
  followed by a `# justification ...` line.
- Adding a new rule: edit `scripts/boundary_check.py`, add a regex constant,
  extend `scan_line()`, add a test in `tests/runtime/test_boundary_check.py`.

## What this phase does NOT do

Deferred to later phases:

- **Phase 3** — actually splitting `~/Lutz_Dev/SAI/` into public + private.
  The boundary linter is the safety net for that split; no real personal
  data lands in the public repo without intent.
- **Phase 4** — the `sai-deploy` skill. The deploy skill will call this
  linter when writing to the public-repo working tree.

## State of the repo after this phase

```
On branch main
New / modified by this phase:
  scripts/boundary_check.py                  (new)
  boundary_check_allowlist.txt               (new)
  .pre-commit-config.yaml                    (new)
  .github/workflows/boundary.yml             (new)
  tests/runtime/test_boundary_check.py       (new, 19 tests)
  PHASE-2-DONE.md                            (this file, new)
```

## Next

Phase 3 — the actual repo split. Walk `/Users/lfinger/Lutz_Dev/SAI/` file
by file, target each at SAI-public (framework + examples) or SAI-private
(real workflows / policies / prompts). Build the `_examples/`
directories in the public repo. Update SAI's runtime entry point to load
from `~/.sai-runtime/` instead of the repo. Keep the old SAI directory
for one rollback cycle, then archive.
