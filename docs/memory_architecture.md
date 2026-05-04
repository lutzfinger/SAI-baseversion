# SAI memory architecture — what we store, where, and why

**Date:** 2026-05-04
**Status:** descriptive (current state) + recommendations (operator
review)

This doc answers the operator's 5th question: how does SAI manage
memory? Where do RAG, databases, stateful, and stateless memory
each fit? Where does canonical YAML memory live in the picture?

---

## TL;DR

| Layer | Where | Lifetime | Read pattern |
|---|---|---|---|
| Stateless prompt memory | `prompts/*.md` (hash-locked) | Session | Loaded per LLM call |
| Canonical YAML memory | `config/*.yaml` (private overlay) | Long-lived | Loaded once + cached per process |
| Stateful event log | `~/Library/Logs/SAI/audit.jsonl` | Append-only forever | Streamed at query time |
| SQLite tables | `~/Library/Application Support/SAI/state/*.sqlite` | Durable across restarts | Indexed query |
| Approval queue | SQLite (`run_store`) | Until resolved + 30d | Queried per ask |
| Proposal queue | `eval/proposed/<wf>/*.yaml` | Until ✅/❌/expire | Directory-scan |
| Eval datasets | `eval/*.jsonl` (per #16a) | Append-only with capped working set | Loaded for regression |
| Pending intent | `eval/pending_intents/*.json` (#16g) | Until closure or expire | One file per intent |

What we DON'T have: RAG / vector store / embeddings. Per #33a any
skill needing semantic-similarity retrieval requires a
primitive-design cycle BEFORE it can be composed into a skill.

---

## The four memory tiers in detail

### 1. Stateless prompt memory (`prompts/*.md`)

Tier prompts (cascade tier system prompts) and agent system
prompts. Loaded fresh on every LLM call via the hash-verifying
loader (`app.shared.prompt_loader.load_hashed_prompt`).

- **Lifetime:** the file's bytes are the memory. Caching is
  process-local (lru_cache); reload on next process start.
- **Access shape:** `body = load_hashed_prompt('agents/...')` —
  fail-closed if hash mismatch (#23 + #24c).
- **Mutability:** edit-then-rehash. The mismatch fails closed
  until the operator updates `prompts/prompt-locks.yaml`.

Why this isn't "real" memory: the prompt doesn't carry state
across invocations. It's a CONFIGURATION snapshot.

### 2. Canonical YAML memory (`config/*.yaml` private)

Operator-curated facts the system needs to make decisions:
courses, TAs, allowlists, registry roles, channel topics, crisis
patterns, runtime tunables.

- **Lifetime:** long-lived. Edited rarely, by the operator via
  Claude Code (per #16e — high-risk surface).
- **Access shape:** Pydantic-validated dict; loader-cached
  (`@lru_cache(maxsize=1)`); `reload()` for tests + runtime
  refresh.
- **Mutability:** edit YAML → next process start picks up. For
  hot-reload, call the module's `reload()` function.

This is the closest thing SAI has to a "knowledge base". It's
small (hundreds of entries, not millions), structured (one
Pydantic schema per kind), and human-auditable (you can grep it).

#### Where canonical memory shines
- **Fail-closed by design.** Missing entry = friendly refusal,
  not silent guess.
- **Operator-auditable.** Every fact has a `last_verified` field;
  staleness sweeps catch drift.
- **Public/private clean.** Schemas + loaders ship in public;
  values live in private (per #17).
- **Test-friendly.** Loaders accept a `reload()` + tests stub the
  path.

#### Where canonical memory is weak
- **O(N) lookup.** `infer_course_from_text` scans every course's
  identifiers; fine for 5 courses, painful at 500.
- **No fuzzy match.** "Mentions COMP-101 in the body" requires
  exact substring; no Levenshtein, no semantic similarity.
- **No cross-file joins.** TA roster references course_id; no
  enforced foreign-key (loader doesn't refuse a TA whose
  course_id isn't in courses.yaml).
- **No versioning beyond git.** A change to a course's late-work
  policy text overwrites in place; the audit log captures the
  edit only if the operator commits + the boundary linter sees
  it.

### 3. Stateful event log (`~/Library/Logs/SAI/audit.jsonl`)

Append-only JSONL. Every gate decision, connector call, approval
transition, and reality observation writes a row (per #4).

- **Lifetime:** forever. Rotated + compacted, never edited.
- **Access shape:** stream-read at query time. The
  `scripts/sai_cost_report.py` + `scripts/sai_metrics_report.py`
  CLIs aggregate this on demand.
- **Why JSONL not SQLite:** writes are concurrent + cheap;
  reads are infrequent + tolerant of full-scan latency.

This is the system's authoritative answer to "what did the system
do." Per #27 (drop-don't-delete) the event log holds the audit
trail of skipped/expired/rejected decisions too.

### 4. SQLite tables (`~/Library/Application Support/SAI/state/`)

Durable state that needs indexed lookup + transactions:

- **`control_plane.db`** — workflow runs, approvals, asks
- **`fact_memory.sqlite`** — operator-confirmed facts (Loop 4
  outputs the system should remember)

Per #3 (approval as durable state): an approval is a row, not a
blocking prompt. Survives restarts.

Per the operator's 2026-05-04 confirmation: SQLite is the default;
no Postgres planned. Single-operator local-first per #1.

---

## What we DON'T have (deliberately, today)

### RAG / vector stores / embeddings

Not in the framework today. ANY skill needing semantic-similarity
retrieval (e.g. "find emails similar to this one", "look up
relevant policy snippet from a 200-page handbook") requires a
PRIMITIVE-design cycle FIRST per #33a:

1. Operator + a Claude session author `docs/design_rag.md`
2. Build `app/canonical/rag.py` (loader, embedder, vector store
   adapter, similarity search) with its own tests
3. Add to `cowork_skill_creator_prompt.md` catalog
4. Then skills can compose it

A skill MUST NOT inline a vector store / embedding call / FAISS
index. The Co-Work skill-creator refuses to emit such a skill.

### Multi-tenant / cross-operator memory

SAI is single-operator (per "what this system is not"). No shared
canonical memory across operators. If a feature only makes sense
when M operators each have their own memory surface, it doesn't
belong here.

### Long-term episodic memory across sessions

The Claude Code memory at
`~/.claude/projects/-Users-lfinger-Lutz-Dev/memory/` is Claude
Code's OWN memory (operator preferences, project facts that I
should remember across MY sessions). It is NOT SAI's memory. SAI
runs on its own clock and has its own state — the two don't
share.

If a SAI skill needs to "remember" something across runs, that's:
- Canonical YAML (if it's a fact the operator owns + edits)
- SQLite fact_memory (if it's a fact the system inferred + the
  operator approved)
- Event log (if it's an audit-shaped trace)

---

## Where canonical memory fits in this picture

Canonical memory is the **operator's curated facts**. It's the
SECOND tier above (between stateless prompts and stateful event
log). It's the "this is what the operator says is true" layer.

The other tiers reference canonical memory:
- **Prompts** can be parameterized over canonical entries
  (e.g. "the operator's allowed L1 buckets are {bucket_list}")
- **Event log** records "we made decision X because canonical
  entry Y said so"
- **SQLite** sometimes joins against canonical (e.g. an approval
  references a course_id that must exist in courses.yaml)

---

## Improvements I'd suggest

### 1. Build a canonical-memory index helper (small framework primitive)

**Problem.** `infer_course_from_text` does O(N × M) scanning
(N courses × M identifiers per course). For 50+ courses with 5+
identifiers each, that's 250+ regex matches per email.

**Proposal.** Add `app/canonical/index.py` — a small
helper that takes any canonical loader and an "identifier extractor"
function, and builds an inverted-index dict on first call. Lookup
becomes O(1) per token.

Mechanism in framework, no per-skill changes needed. The same
helper covers TA-by-email lookup (currently O(N)) + sender-by-domain
(currently O(N) over domains).

**SHIPPED in this session** as `app/canonical/index.py` — see
below.

### 2. Add a staleness-sweep CLI

**Problem.** Each canonical schema has a `last_verified` /
`policy_last_verified` field. Operator may drift away from updating
them; nothing surfaces stale entries periodically.

**Proposal.** `python -m scripts.sai_canonical_check` — text-mode
CLI listing every canonical file with at least one entry's
`last_verified` older than the file's max-age tunable. Operator
runs weekly; output goes to sai-status channel via a future cron.

Defer to next session — the staleness-check pattern needs a
small Pydantic introspection helper first.

### 3. Foreign-key validation across canonical files

**Problem.** A TA's `course_id` can reference a course that
doesn't exist in courses.yaml. The loader doesn't refuse; the
runtime fails when `get_active_tas_for_course("missing")` returns
empty.

**Proposal.** Add an inter-file validation step that runs at
process startup (or via `sai-canonical-check`). Schema:

```yaml
# In a future config/canonical_constraints.yaml:
constraints:
  - { from: teaching_assistants.course_id, to: courses.course_id }
  - { from: skills/<wf>/manifest.feedback.channel, to: channel_allowed_discussion.<channel>.allowed_topics[*].kind }
```

Loader enforces; mismatches fail closed at process start.

Defer — bigger change; want operator's input on the constraint
expression syntax.

### 4. Versioned canonical edits via the audit log

**Problem.** Editing a course's late-work policy in YAML
overwrites in place. The audit log doesn't capture the diff
unless someone notices.

**Proposal.** Add an `app/canonical/audit.py` watcher that
hashes every canonical file at process start; compares to the
last-recorded hash in `~/Library/Logs/SAI/audit.jsonl`; on
mismatch writes an audit row with the diff summary.

Defer — small but needs careful design (don't want to flood the
audit log with every edit during operator iteration).

---

## What shipped in this session

- `app/canonical/index.py` — see new file. Generic inverted-index
  helper for any canonical loader.
- `tests/canonical/test_index.py` — coverage for the helper.

The other improvements (staleness sweep, FK validation, version
audit) are noted as next-session candidates.
