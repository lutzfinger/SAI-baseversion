# `eval/` — placeholders for the operator's evaluation datasets

**Per PRINCIPLES.md §16a + §16d:** every workflow ships with three
mandatory eval datasets (canaries, edge_cases, workflow_regression).
Eval is the framework's reason to exist (#10 — "Eval-centric
architecture").

This directory holds **PLACEHOLDER files** with one synthetic example
each so stranger installs can see the shape. The operator's PRIVATE
overlay overrides these with real data — per #17 (public ships
mechanism, private ships values) and the operator's 2026-05-04
clarification ("EVAL = PRIVATE; pub. should have placeholders since
we do EVAL FIRST on any workflow, but they're not filled out").

## What lives here

| File | Purpose | When the cascade reads it |
|---|---|---|
| `canaries.jsonl` | one synthetic email per rules-tier rule | every Loop 4 apply (regenerated from rules) |
| `edge_cases.jsonl` | LLM-tier soft-fail regression set | every apply, after canaries |
| `disagreement_queue.jsonl` | local-vs-cloud disagreement queue (Loop 2) | weekly batch surface |

## What does NOT live here

- **Real operator data** — those rows live in PRIVATE overlay
  (`$SAI_PRIVATE/eval/*.jsonl`). The merge gives private
  precedence on path conflicts.
- **Per-skill regression sets** — those live in
  `app/skills/<workflow_id>/{canaries,edge_cases,workflow_regression}.jsonl`
  (per #33 skill plug-in protocol). The operator's skills land in
  the private overlay's `skills/` directory.
- **Process-level eval** (e.g. how the skill-creator behaves) —
  that's `$SAI_PRIVATE/eval/skill_creator_regression.jsonl`,
  private.

## How a stranger uses this

1. Clone the public repo.
2. The placeholder files here let the framework's regression
   gates run (won't fail on "missing required eval file").
3. Replace the synthetic placeholder rows with real data as the
   operator labels emails / corrects classifications / curates
   edge cases.
4. As the operator's data grows, the eval files grow. The shape
   stays the same; only the rows change.

## Why one synthetic example, not zero

If the file were truly empty, the regression gate would pass
trivially (0 cases = 0 failures = "all pass"). One synthetic
example forces the cascade to actually walk + the validator to
actually fire. Catches "I broke the loader" / "my regex
regressed" type bugs even before the operator has real data.
