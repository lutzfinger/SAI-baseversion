# Design: live public + skill-sync versioning architecture

**STATUS:** v0.1 SHIPPED 2026-05-04 (skill-content integrity hash +
promote CLI). Framework-versioning + deprecation policy stage 2 is
specified here but not yet enforced — ship when SAI has more than
one external user OR a second non-trivial skill exists, whichever
first.

**Maps to:** PRINCIPLES.md §17 (public mechanism / private values),
§23 (hash-verified loading, fail-closed), §24c (prompts are
content-addressed), §32 (test before action; smoke before cutover),
§33 (skill plug-in protocol), §33a (skills compose primitives —
primitives are separate work).

**Closes:** operator's two architectural questions of 2026-05-04:
- "How do we ensure that while I am writing new functions we don't
  break other existing systems?" (this doc, sections A–C)
- "Skills are created in Claude Co-Work and used by SAI. SAI
  downloads them and uses them. How does SAI understand when a
  skill in Claude has changed?" (this doc, sections D–E; shipped
  in `app/skills/integrity.py` + `scripts/promote_skill.py`)

---

## A. The problem

SAI is local-first today (one operator on one Mac), but the public
repo IS the framework — same code stranger installs would clone.
That makes every commit a potential breaking change for:
- the operator's own running daemon (live email triage, future RAG
  agent),
- any future stranger install that pulled an earlier release.

Two kinds of breakage to prevent:
1. **Framework breakage:** a primitive's API changes; existing skills
   that compose it stop working.
2. **Skill breakage:** a skill's contents drift after promotion (an
   operator edits the runner in place, or a re-merge clobbers the
   wrong file); SAI's behaviour silently changes without an audit
   trail.

The first is solved by versioning + deprecation discipline (sections
B–C). The second is solved by content-hash integrity (sections D–E,
shipped today).

---

## B. Framework versioning (semver)

**Rule:** `pyproject.toml::version` is semantic.

| Part of API | What change requires what version bump |
|---|---|
| Public symbols re-exported from `app/<pkg>/__init__.py` | Removing or renaming → MAJOR. Adding → MINOR. |
| Argument signatures of those public symbols | Removing positional arg or renaming a kwarg → MAJOR. Adding optional kwarg with default → MINOR. |
| Pydantic model fields (`SkillManifest`, `Document`, `QueryResult`, `LLMRequest`, etc.) | Removing required field → MAJOR. Adding required field → MAJOR. Adding optional field with default → MINOR. Renaming → MAJOR (alias acceptable as MINOR for one cycle). |
| `app/cascade/runner.py::run_cascade` ABI | Any change to the `manifest`/`inputs`/`extra` contract → MAJOR. |
| Channel registry topic `kind` strings | Removing → MAJOR. Adding → MINOR. |
| Tier kinds in `SkillManifest.cascade[].kind` | Removing → MAJOR. Adding → MINOR. |
| Internal helpers (single-underscore prefix), tests, scripts | PATCH. |
| Bug fixes that don't change documented behavior | PATCH. |

**Pre-1.0 (today):** anything goes — but every cycle bumps minor.
Once 1.0 ships (target: when first non-operator user lands), the
rules above are binding.

---

## C. Don't-break-existing checklist (Track 0 — every PR)

Required CI for every change (today: manual; after this doc ships,
in `.github/workflows/ci.yml`):

1. **Boundary linter** — `python scripts/boundary_check.py` clean.
2. **Full pytest** — `pytest -q` clean.
3. **Skill-protocol contract test** — load the public sample skill
   (`app/skills/sample_echo_skill/`) + run its full
   workflow_regression.jsonl through the cascade. Catches the
   common breakage shape: a skill that was working stops working.
4. **Manifest schema-contract test** — `SkillManifest` round-trips
   the sample skill's `skill.yaml` without drift. Catches
   breaking changes to the skill schema (which would orphan every
   external skill).

Track 0 lives in `tests/test_cascade_framework_e2e.py` + the
sample-skill regression cases. Already shipped as of v8.1.

**Optional but encouraged:**

5. **Operator's e1 regression** — for changes that touch
   `app/canonical/*`, `app/llm/*`, or `app/cascade/*`, the operator
   re-runs `cornell-delay-triage`'s `workflow_regression.jsonl`
   against real Anthropic before merging. Doesn't gate CI (private
   data) but the change risks deferring or breaking the operator's
   live workflow.

**Deprecation policy** (post-1.0):
- Symbol marked deprecated in MINOR release with a `DeprecationWarning`.
- Removed no earlier than the second MAJOR release after the
  deprecation. Concretely: deprecated in 1.3 → still works in 2.x →
  removable in 3.0.
- `MIGRATION-PRINCIPLES.md` (private) tracks pending deprecations
  per cycle; `CHANGELOG.md` (public, future) records each one.

---

## D. Skill-content integrity hash (shipped today)

**The mechanism:** `app/skills/integrity.py` computes a deterministic
SHA-256 over the files the framework EXECUTES (manifest + runner +
send_tool + eval files + prompts + config-diffs). Excluded: README,
__pycache__, .bak files, .DS_Store, MANIFEST.txt (Co-Work's own,
which would be circular).

At promotion time (`scripts/promote_skill.py`), the hash is computed
and written to `<skill_dir>/.skill-content-sha256`. The skill loader
re-computes on every load and refuses to register a skill whose
contents drift from the recorded hash.

This answers the operator's question:

> "Skills are created in Claude (Co-Work) and are in Claude. SAI
> uses them. SAI downloads them and uses them. How does SAI
> understand when a skill in Claude has changed?"

→ Co-Work emits a draft. Operator drops it at
`incoming/<draft_id>/`. Claude Code runs `promote_skill` which
validates, stamps the hash, and moves to `skills/<workflow_id>/`.
On every SAI startup (and every cascade run), the loader verifies
the stamped hash. **If anyone edits the skill after promotion —
Co-Work pushed a new version, an operator hand-edited a regex,
a re-merge clobbered something — SAI fails closed and surfaces the
drift.**

**Reverse path** (operator deliberately edits a skill in place):
```sh
python -m scripts.promote_skill --in-place \\
    --incoming-dir ~/Lutz_Dev/SAI/skills/cornell-delay-triage/ \\
    --target-dir   ~/Lutz_Dev/SAI/skills/cornell-delay-triage/
```
Re-stamps the integrity hash to the current contents. Auditable
(the audit log records WHO ran promote_skill WHEN; the file
mtime + content hash combine to make tampering visible).

**Migration path:** existing skills (today: just
`cornell-delay-triage`) don't have the `.skill-content-sha256`
file yet. Loader behavior:
- **Phase 1 (today, SHIPPED):** integrity check is OPT-IN. Loader
  doesn't auto-verify. Operator runs `promote_skill --in-place` to
  stamp.
- **Phase 2 (after stamping all known skills):** loader runs
  `verify_skill_integrity(skill_dir, strict=False)` and logs drift
  to the audit log without blocking.
- **Phase 3 (post-1.0):** loader runs `strict=True` — fail closed
  on drift.

Phase 2 ships when the operator has stamped their existing skill
(one-line command). Phase 3 ships post-1.0.

---

## E. Skill versioning (semver per skill)

Every `SkillManifest` declares `identity.version` (semver). The
operator + Co-Work follow the same rules as B above, scoped to the
skill:

| Skill change | Bump |
|---|---|
| Add a new cascade tier; remove one; reorder | MAJOR |
| Change a tier's `kind` (rules → cloud_llm) | MAJOR |
| Add a required tier config key | MAJOR |
| Tighten a verdict enum (e.g. drop a verdict value) | MAJOR |
| Add a new optional cascade tier config key | MINOR |
| Add a new output (no new side effect class) | MINOR |
| Loosen a verdict enum / classifier (additive) | MINOR |
| Prompt edits | PATCH (but bump prompt-locks hash) |
| Drafter copy edits, eval case additions | PATCH |
| Bug fixes in `runner.py` that don't change behaviour | PATCH |

**Skill version + integrity hash are independent dimensions:**
- version answers "what's the API contract this skill claims?"
- integrity hash answers "is what's on disk the same bytes that were
  promoted under that version?"

A skill at v0.2.2 with a drifted integrity hash means someone
edited it in place. The framework refuses to load it (Phase 3) /
warns (Phase 2) / says nothing (Phase 1, today).

---

## F. What this doc does NOT do

- **No CHANGELOG generation.** Future: when stranger users land,
  add `tools/changelog.py` that derives a CHANGELOG from PR titles
  + git tags. Not blocking.
- **No CI for the operator's private skill regression.** That's the
  operator's call (their data). The framework's CI gates the public
  repo only.
- **No automatic deprecation warnings on MINOR removals.** Pre-1.0;
  add when 1.0 ships.
- **No rollback tooling.** Operator's skill workflow already has
  `.pre-vX.Y.Z.bak` directories from previous promotes; that's the
  manual rollback path. Auto-rollback on integrity failure is
  v2.x.

---

## G. Operator-side workflow (today)

```sh
# 1. Operator + Co-Work design a new skill in Co-Work session.
# 2. Co-Work emits a tar.gz with skill.yaml + runner.py + eval files
#    + prompts.
# 3. Operator extracts to ~/Lutz_Dev/SAI/skills/incoming/<draft_id>/

# 4. Claude Code (or operator manually) validates + integrity-stamps
#    + moves:
python -m scripts.promote_skill \\
    --incoming-dir ~/Lutz_Dev/SAI/skills/incoming/some-draft/ \\
    --target-dir   ~/Lutz_Dev/SAI/skills/some-workflow/

# 5. Re-merge the overlay so the runtime picks it up:
sai-overlay merge --public ~/Lutz_Dev/SAI-baseversion \\
                  --private ~/Lutz_Dev/SAI \\
                  --out ~/.sai-runtime --clean

# 6. Bot reloads on next iteration; cascade walks the new skill.
```

When Co-Work ships a NEW VERSION of a skill the operator already
has installed:
1. Same promote step into `incoming/<draft_id>-vNEW/`.
2. Operator backs up old: `mv skills/<id> skills/<id>.pre-<old>.bak`.
3. `promote_skill --target-dir skills/<id>/` (now empty).
4. Re-merge.
5. The integrity hash is fresh; old version preserved as `.bak`.

---

## H. Why not Pinecone-style remote registry today?

Considered + rejected for v0.x:
- **Cost:** stranger installs would need an account.
- **Latency:** local hash check is microseconds; remote API call is
  hundreds of ms per skill load.
- **Supply chain:** a remote registry IS the supply chain attack
  surface.
- **Simplicity:** local files + a hash file are trivially auditable
  with `cat .skill-content-sha256`. A remote registry needs
  authentication, network availability, and trust in a third party.

Local file + integrity hash is right for v0.x. Reconsider if SAI
ever gets a marketplace shape (skills published by multiple
authors, discoverable + installable cross-operator).
