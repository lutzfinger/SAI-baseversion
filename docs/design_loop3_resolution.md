# Design — Loop 3 (resolution → adjust → witness)

**Status:** explanation for operator review (you flagged "needs
operator definition of witness")
**Audience:** operator
**Maps to:** PRINCIPLES.md §16a (the four loops); MIGRATION-
PRINCIPLES.md audit item B8 (Loop 3 zero implementation).

---

## What Loop 3 IS in PRINCIPLES.md

From §16a, the four loops are:

1. **Loop 1 — Pre-ship regression** (canaries + edge_cases gate every
   code change). ✅ shipped.
2. **Loop 2 — Disagreement triage** (operator batches local-vs-cloud
   disagreements). ✅ data side shipped (queue + curator);
   batch-ask UI is partial.
3. **Loop 3 — Resolution → code change → regression → witness.**
   THIS DOC. Triggered when a Loop 2 batch comes back resolved.
   Look at verdict patterns; adjust code (rules YAML edit, prompt
   addendum); run Loop 1; add the **witnesses** — the subset of
   resolved disagreements that best capture the lesson — to the
   appropriate dataset (A canaries if rule, B edge_cases if LLM
   hint). Not all resolved disagreements; only canonical witnesses.
   Consumer: nobody (autonomous), full audit log.
4. **Loop 4 — Operator-driven** (sai-eval Slack patterns). ✅
   shipped.

The principle nails down what Loop 3 should DO; what's open is the
**definition of "witness"** — i.e., once a batch comes back, which
of the resolved rows graduate into the eval datasets, and how do
we pick them?

---

## The "witness" question — three candidate definitions

### Definition A — All-resolved-graduate-as-witnesses

Every resolved disagreement becomes a witness in the appropriate
dataset (canaries if a new rule was added; edge_cases otherwise).

**Pros:** simplest. No selection logic.
**Cons:** dataset bloat. If a 50-row batch resolves and 40 of them
are minor variations on the same theme, you get 40 near-duplicate
edge_cases. Defeats the soft-cap.

### Definition B — Cluster-and-pick (recommended starting point)

After a batch resolves, cluster the rows by:
- (sender_domain, expected_l1) for rule-track resolutions
- (subject-prefix-keyword, expected_l1) for LLM-track resolutions

Pick the **earliest-occurring representative** of each cluster as the
witness. Dataset gets one row per cluster, not one per disagreement.

**Pros:** keeps dataset growth proportional to NEW patterns, not
volume. Aligns with the soft-cap discipline (#16a).
**Cons:** "cluster" needs a metric. Sender_domain is good for rule-
track; "subject-prefix-keyword" is fuzzy for LLM-track.

### Definition C — Operator picks at resolution time

When the operator answers a Loop 2 batch ask in Slack, they have a
"witness this" reaction (e.g., 🎯 or `+w`) on each row they want
graduated. Default: NO graduation unless explicitly marked.

**Pros:** operator control; no machine clustering needed.
**Cons:** more friction at resolution time; relies on operator
discipline (which we know isn't always there).

---

## Recommended approach: B with a fallback to A

1. **Default:** cluster as in B; pick representatives. Soft-cap
   honored: if dataset is at SOFT_CAP, evict the most-redundant
   row in the same cluster before adding the new representative.
2. **Operator override:** the Loop 2 batch ask UI lets the operator
   tag any row with 🎯 to force-graduate it (definition C added on
   top of B). Force-graduates skip the cluster check.
3. **Audit:** every graduation writes a `WitnessRecord` row to
   `eval/witness_log.jsonl` with: `disagreement_id`,
   `graduated_to` (canaries|edge_cases), `selection_method`
   (cluster_representative|operator_force), `cluster_id` if any.
4. **Promotion rule (already in §16a):** when a NEW rule is added
   in Loop 3, sweep edge_cases for any rows the new rule covers at
   ≥ production confidence; remove them from B (now redundant —
   A covers them). Net direction: B shrinks as rules absorb cases.

---

## Module shape

```
app/eval/loop3/
  __init__.py
  resolver.py          # consumes a resolved batch → adjustments + witnesses
  cluster.py           # clustering helpers
  witness.py           # WitnessRecord schema + audit log
  promotion.py         # rule-promotion sweep (rule added → drop redundant edge_cases)

scripts/
  run_loop3.py         # CLI: run on a resolved batch JSONL
```

```python
class ResolutionResult(BaseModel):
    """One Loop 3 run on one resolved batch."""

    batch_id: str
    code_changes_proposed: list[ProposedCodeChange]   # rule_add | prompt_addendum
    witnesses_added_to_canaries: list[CanaryRow]
    witnesses_added_to_edge_cases: list[EdgeCaseRow]
    edge_cases_evicted_by_promotion: list[str]        # edge_case_ids
    audit: list[WitnessRecord]


def resolve_batch(
    *, batch_path: Path, runner: Any,
) -> ResolutionResult:
    """Load resolved batch → cluster → propose code change → run
    Loop 1 (regression gate) → on green, materialize witnesses +
    sweep edge_cases for promotion."""
```

---

## End-to-end flow (proposed)

```
Operator answers a Loop 2 batch ask in Slack
   │
   │ (resolved rows live in eval/disagreement_queue.jsonl with
   │  resolution metadata)
   │
   ▼
[run_loop3.py] (cron daily OR triggered by batch resolution)
   │
   ├─ Load resolved rows
   │
   ├─ Cluster (definition B)
   │
   ├─ For each cluster:
   │     - propose code change (add rule | add prompt addendum)
   │     - pick witness (cluster representative)
   │
   ├─ Run Loop 1 (canaries + edge_cases regression) on the
   │   PROPOSED code state
   │
   ├─ If regression passes:
   │     - apply code change (uses existing apply_proposal path)
   │     - add witness to appropriate dataset
   │     - sweep edge_cases for promotion (rule_add only)
   │     - log everything to witness_log.jsonl
   │
   └─ If regression fails:
        - skip the change; mark batch as needing re-review
        - post Slack message to operator with the diagnostic
```

---

## Hard rules

1. **Loop 3 NEVER runs without operator-resolved input.** The
   resolution data MUST come from a Loop 2 batch the operator
   answered. No autonomous "the cloud said so, let's graduate it"
   shortcut.
2. **Code changes via Loop 3 use the SAME apply path as Loop 4.**
   Same staging, same regression gate, same backup/rollback. The
   only difference: Loop 3 doesn't need the operator's ✅ on each
   change because the batch resolution already encoded approval.
3. **Cap on Loop 3 changes per run.** Default 5 per batch; if the
   batch surfaces more than 5 distinct clusters needing changes,
   log + escalate to operator (don't auto-apply 20 changes silently).
4. **Witness graduation is one-way.** A witness in canaries or
   edge_cases cannot be removed by Loop 3 without an explicit
   operator action (operator can edit the JSONL manually OR via
   Loop 4 `remove rule`).

---

## Eval contract

Loop 3 itself needs a `workflow_regression.jsonl` (it IS a workflow
under the §33 protocol). Cases:

```jsonl
{"case_id": "all_one_cluster", "batch": [...], "expected_witnesses": 1}
{"case_id": "two_distinct_clusters", "batch": [...], "expected_witnesses": 2}
{"case_id": "promotion_evicts_redundant_edge_cases", "batch": [...], "expected_evictions": 3}
{"case_id": "regression_fails_no_changes_apply", "batch": [...], "expected_outcome": "no_apply"}
```

---

## Open questions for operator review

1. **Witness definition** — A, B, or C? (I propose B + force-
   graduate, hybrid.)
2. **Cluster metric for LLM-track resolutions.** Sender_domain
   works for rule-track. For LLM-track (where the resolution adds a
   prompt addendum or an edge_case), what's the cluster key?
   Candidates:
   - subject keyword + expected_l1
   - sender_domain + expected_l1
   - random hash of body excerpt (no clustering, all become
     witnesses — definition A for LLM-track)
   I lean **subject keyword + expected_l1** as the v1.
3. **Daily cap on auto-applied changes.** I propose 5 per batch, 10
   per day across all batches. Above the cap → escalate to operator.
4. **Trigger model.** Cron daily at 6am OR push-triggered when a
   Loop 2 batch flips to resolved? Cron is simpler; push is more
   responsive. I lean cron daily for v1.
5. **The "auto-apply" question.** Loop 4 always requires operator
   ✅. Loop 3 (per the principle) is "consumer: nobody (autonomous)
   — full audit log." Confirming you want Loop 3 to apply WITHOUT
   per-change operator ✅ — relying on the regression gate +
   audit log + the fact that the operator already approved the
   underlying batch resolution.

---

## Effort

~2 sessions to ship Loop 3 fully:

- Session 1: cluster.py + witness.py + resolver.py + tests + Loop 3
  workflow_regression.jsonl
- Session 2: promotion.py + integration with Loop 1 + the auto-apply
  path + the cron + ops dashboard hooks

Worth doing AFTER e1 + e2 ship — operator's hands-on signal pattern
will inform the cluster metric choice. Premature cluster design risks
solving the wrong problem.
