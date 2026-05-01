# Email classifier QA workflow (template)

This is a **template** for the operator's private QA runbook. Copy to
private at `docs/email_classifier_qa.md` and adapt the paths,
thresholds, and source-of-truth doc references.

The runbook exists to prevent drift between three things that have to
stay in sync:

1. The **taxonomy doc** (`docs/email_taxonomy.md`) — what each bucket
   *means*.
2. The **keyword rules** (`prompts/email/keyword-classify.md`) — the
   deterministic shortcuts that resolve at the rules tier.
3. The **LLM prompts** (`prompts/email/llm-classify-*.md`) — what the
   LLM tier emits when rules abstain.

When these three drift, the cascade silently mislabels mail. The QA
runbook is the seatbelt.

## What a QA workflow needs

### Step 1 — Read the source-of-truth docs (REQUIRED)

The single most common drift cause is "I assumed I knew what `<bucket>`
meant." A reader who skips this step risks proposing a rule that uses
a bucket name that doesn't exist in the Literal, or one that contradicts
an existing doc rule.

The QA script (see step 2) enforces this by printing SHA256 hashes of
each source-of-truth doc at startup and refusing to proceed if any are
missing. The reader still has to actually open the docs and read them —
the hashes are just receipts.

### Step 2 — Run the QA script against the labelled dataset

The script runs the cascade in three modes:

- **Rules-only** — measures the keyword baseline's coverage and precision
- **Local LLM independent** — measures how much the local model can
  recover when rules abstain
- **Cloud LLM independent** — measures the ceiling

Same dataset, all three tiers, apples-to-apples. Reports per-bucket
P/R/F1 for each tier so per-bucket regressions are visible even when
overall accuracy is stable.

### Step 3 — Cross-check rules against the schema

Every bucket name referenced anywhere (rules YAML, LLM prompts) must
exist in the canonical Literal type
(`Level1Classification` in `app/workers/email_models.py`). The script
reads the Literal and validates every keyword-baseline entry against
it. Mismatches fail the run.

This catches the "I made up a bucket name" failure mode at QA time
instead of in production.

### Step 4 — Decide and act on proposals

The script prints proposed rule additions but does NOT auto-apply.
PRINCIPLES #20 — reflection may suggest, never auto-apply. The reader
reviews each proposal against the doc and decides.

Reject proposals that:
- Use a bucket name not in the Literal
- Contradict an existing doc rule (operator must update the doc first)
- Are based on a pattern the reader can't articulate in one sentence

### Step 5 — Run integration tests

Every cascade-runner change must pass the public integration test plus
any operator-specific keyword classifier tests. The QA workflow is a
gate; tests are the verification that the gate works.

### Step 6 — Commit + write phase report

Public + private commit separately (PRINCIPLES #25). Each commit message
quotes the doc section that justifies the change and cites the QA-report
SHA256 (proof QA ran).

### Step 7 — Update the baseline pointer

QA runs save versioned reports. The latest report becomes the comparison
point for the next run. Drops > 5pp on overall accuracy or any bucket's
F1 require explicit operator sign-off before committing.

## Triggers — when to run

- Any edit to keyword baseline (`prompts/email/keyword-classify.md`)
- Any edit to the LLM prompts (`prompts/email/llm-classify-*.md`)
- Any edit to the taxonomy doc (`docs/email_taxonomy.md`)
- Any edit to `Level1Classification` in `email_models.py`
- Routine sanity sweep every 2 weeks

## What this catches

1. **Bucket name typos** that would otherwise hit production
2. **Per-bucket regressions hidden by stable overall accuracy**
3. **Doc/prompt/rules drift** (via SHA256 deltas)
4. **LLM tier hallucinations** that emit bucket names outside the Literal

## What it doesn't catch yet

- L2 (intent) drift — currently L1-only
- Per-thread consistency
- Inbox-distribution drift (you start a new job, etc.)

## Reference: the failure mode this workflow exists to prevent

When the QA workflow ships in the operator's private overlay, document
ONE specific real failure that motivated it. Concrete failures keep
the runbook from being abstract.
