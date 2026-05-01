# Phase 3F runbook: cutover from private SAI → merged runtime tree

This is interactive. You run the steps; I prepared the helper script and
the eject buttons. Estimated time: 30 minutes the first time, 5 minutes
on re-runs.

## Pre-cutover state

Today, the launchd job `com.sai.tag-new-inbox` invokes:

```
WorkingDirectory  = $SAI_PRIVATE
ProgramArguments  = $SAI_PRIVATE/scripts/run_tag_new_inbox.sh
```

That runs the OLD `email_triage.py` cascade in private SAI, ignoring all
the new AI-Stack architecture in public SAI-baseversion.

## Goal

After cutover:

```
WorkingDirectory  = ~/.sai-runtime
ProgramArguments  = ~/.sai-runtime/scripts/run_tag_new_inbox.sh
```

The runtime tree is built by `sai-overlay merge` — public + private with
private winning on conflicts. Hash-verified against the manifest at
runtime by `app/runtime/verify.py`.

## Side-by-side period (recommended, 1 week)

Don't flip launchd immediately. Run the merged runtime by hand alongside
the existing private SAI, compare EvalRecord output to reality, then
flip when comfortable.

### Day 0: build the runtime

```sh
cd $SAI_PRIVATE-baseversion
make install                              # ensures sai-overlay is on PATH
scripts/sai_cutover.sh --status           # see current state
scripts/sai_cutover.sh --build            # produces ~/.sai-runtime
```

Expect output like:
```
build ok: 682 files, 13M
verify ok: ~/.sai-runtime
```

If that says anything else, stop and read the error. Most likely cause:
private has a `logs/` dir or `quarantine/` dir that wasn't pruned —
those are skipped by the overlay tool but if your private repo has a
nonconventional layout you'll see warnings.

### Day 0 → 7: smoke test by hand

Before flipping launchd, run the merged tree manually for one task:

```sh
cd ~/.sai-runtime
# Use private's venv, since it already has all real deps installed
$SAI_PRIVATE/.venv/bin/python -m app.workers.tag_new_inbox
```

Watch what writes to `~/Library/Logs/SAI/` and `~/Library/Application Support/SAI/state/`.
Compare against what private SAI is currently producing in the same paths.

If outputs match (or differ only in expected ways), the merged runtime
is healthy. If they diverge, **don't flip yet** — file the divergence
and figure out which task migration is needed.

### Day 7: flip the launchd job

Pre-flip checks:

```sh
scripts/sai_cutover.sh --status            # confirm baseline
ls -la ~/Library/LaunchAgents/com.sai.tag-new-inbox.plist
cp ~/Library/LaunchAgents/com.sai.tag-new-inbox.plist /tmp/sai-launchd-backup.plist  # rollback safety
```

Run the build:

```sh
scripts/sai_cutover.sh --switch            # unload launchd + build runtime
```

Edit `~/Library/LaunchAgents/com.sai.tag-new-inbox.plist`:

```xml
<key>WorkingDirectory</key>
<string>/Users/example/.sai-runtime</string>     <!-- was $SAI_PRIVATE -->

<key>ProgramArguments</key>
<array>
    <string>/Users/example/.sai-runtime/scripts/run_tag_new_inbox.sh</string>
</array>
```

(The script template at `scripts/launchd/com.sai.log-maintenance.plist.template`
is the same shape — substitute `__SAI_REPO_ROOT__` with `~/.sai-runtime`.)

Reload:

```sh
scripts/sai_cutover.sh --reload
launchctl list | grep com.sai             # should show as loaded
```

### Day 7 → ∞: monitor

Tail the launchd output and SAI logs. You should see runs land in
`~/Library/Logs/SAI/scheduled/launchd_tag_new_inbox.{out,err}.log`.
Watch for the first scheduled fire (or trigger manually with
`launchctl kickstart -k user/$UID/com.sai.tag-new-inbox`).

If anything goes wrong, **rollback is one command**:

```sh
scripts/sai_cutover.sh --rollback
# then edit the plist back to point at $SAI_PRIVATE
scripts/sai_cutover.sh --reload
```

You kept `/tmp/sai-launchd-backup.plist`; just copy it back over.

## Re-merging after a public push or private edit

```sh
cd $SAI_PRIVATE-baseversion
git pull                                          # pull public updates
cd $SAI_PRIVATE && git pull                     # pull private updates
cd $SAI_PRIVATE-baseversion
scripts/sai_cutover.sh --build                    # re-merge to ~/.sai-runtime
```

The merged runtime is rebuilt cleanly each time (the script passes
`--clean` to `sai-overlay merge`). Manifest re-hashes; loader re-checks
on next launchd fire.

## What `~/.sai-runtime` actually contains

After a successful build:

```
~/.sai-runtime/
  .sai-overlay-manifest.json   # SHA-256 hash of every file (verified at startup)
  app/                         # framework + private overrides
  config/                      # private config (your team_members.yaml etc)
  prompts/                     # private prompts (your real L1 buckets)
  policies/                    # private policy thresholds
  workflows/                   # private workflow YAMLs
  registry/                    # public registry + private overrides
  scripts/                     # public + private shell scripts
  tests/                       # both test suites (mostly private)
  ... etc
```

Runtime state still lives at `~/Library/{Logs,Application Support}/SAI/`.
The merged tree is read-only at runtime; SAI never writes back to it.

## Troubleshooting

**`sai-overlay: command not found`**
Re-run `make install` in `$SAI_PRIVATE-baseversion`. The script is
installed by the editable install but goes stale if your venv is rebuilt.

**`verify failed (manifest mismatch)`**
Something wrote to `~/.sai-runtime/` after the manifest was generated —
shouldn't happen if you're not editing the merged tree manually. Re-run
`scripts/sai_cutover.sh --build` to rebuild from scratch.

**launchd job stops firing after cutover**
Run `launchctl list com.sai.tag-new-inbox` and check the last exit code.
If non-zero, tail `~/Library/Logs/SAI/scheduled/launchd_tag_new_inbox.err.log`.
Most likely: a private overlay file expects a path that didn't merge
correctly. Rollback, file the issue.

**The new AI-Stack architecture isn't being used yet**
Right. The cutover only changes WHERE the existing private code runs from
(merged tree instead of private repo). It doesn't change WHICH code runs.
For the launchd job to actually use the new `TieredTaskRunner`, you need
to migrate `app/workers/tag_new_inbox.py` (private) to call the runner
instead of the old cascade. That's the per-task migration work in
MIGRATION-BACKLOG.md — start with email_classification.

## What this gets you

- A reproducible runtime tree built from two source repos.
- Hash-verified loading: tampering or unregistered files fail-closed
  before the control plane starts (Phase 1's verifier reads the manifest
  this tool produces).
- Public/private boundary enforceable: `boundary_check.py` runs on
  public; private is private; the merged tree picks the right file.
- Public can be open-sourced without leaking private data.
- Backout is one command + a plist edit.
