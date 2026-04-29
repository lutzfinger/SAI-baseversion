# Local Model Rollout Loop

This note defines how SAI should calibrate, train, and promote the local email
classifier relative to the cloud classifier and real operator outcomes.

## Current Repo State

As of April 1, 2026, the repo is only partially aligned with the intended
rollout loop.

Implemented today:

- The email-style workflows currently run in a cloud-backed calibration mode.
  Their escalation tool configs still set `always_run: true`, so the cloud
  classifier runs on every item even though a `confidence_below` threshold is
  also present.
- When the cloud classifier returns a result, that cloud result becomes the
  in-the-moment final classification for the workflow run.
- The system records:
  - the keyword baseline
  - the prior local prediction
  - the cloud target
  - the final classification used in the workflow
- The system also augments the learning corpus with real operator-outcome
  signals such as confirmed replies and outcome failures.
- Local prompt tuning runs in 50 fresh-example increments. In steady state that
  means 50, 100, 150, 200, 250, 300, ... and not 299 after the 250 batch.
- Local LoRA preparation starts at 300 unique training examples.
- Local LoRA remains local-only. No cloud fine-tuning upload is allowed.

Not implemented today:

- No clean future cutoff is enforced between training data and promotion
  evaluation data.
- No automatic promotion gate compares local vs. cloud on a post-cutoff
  operator-outcome dataset.
- No automatic switch to local-first happens when local reaches the promotion
  threshold.
- No low-confidence-only cloud spot-check mode is active yet, because
  `always_run: true` still keeps cloud on every item.

## Intended Rollout Policy

### Phase 1: Cloud-Always-On Calibration

Start with cloud-always-on.

- The local model still runs on every item so SAI can observe where it differs.
- The cloud model is the de-facto operational standard for immediate decisions
  during this phase.
- The final workflow decision should continue to follow the cloud result while
  calibration is active.

The goal of this phase is not to save cloud calls yet. The goal is to build a
high-quality supervised dataset.

### Training Data Sources

The training signal is not just local vs. cloud disagreement.

SAI should track three things:

1. The local model prediction
2. The cloud model prediction
3. What really happens afterward

Interpretation:

- Cloud is the temporary operational standard during calibration.
- What really happens is the strongest correctness signal.
- Real operator behavior is the long-run source of truth for promotion
  decisions.

That means the training corpus should combine:

- cloud-backed disagreement examples
- operator-approved label-correction examples
- operator-confirmed outcome examples
- later high-quality reviewed traces when they are explicitly marked as perfect

Operational hygiene rule:

- the active training dataset should be rebuilt from the clean source logs, not
  treated as the long-term source of truth itself
- `cloud_target` rows come from the local-vs-cloud comparison log
- operator-confirmed and operator-correction rows come from their dedicated
  append-only logs
- clearly synthetic fixture rows should be quarantined out of the live corpus
  before prompt-tuning or LoRA thresholds are evaluated

Prompt tuning and LoRA should both learn from that combined corpus, with source
provenance preserved in each record.

Prompt-tuning cadence should stay explicit:

- prompt tuning consumes the next 50 fresh records each time a new 50-example
  milestone is reached
- after the 250 batch has already run, 299 total examples should not trigger a
  prompt-tuning run
- 300 is the next shared milestone, where prompt tuning can take the next 50
  fresh examples and the LoRA stage can first become eligible

### Clean Future Cutoff

Promotion must use a clean future cutoff.

Definition:

- Everything before the cutoff can be used for prompt tuning and LoRA training.
- Everything after the cutoff is held out for evaluation only.

The held-out side must be future data, not a reshuffled sample of earlier
training rows. This avoids promoting a local model based on memorized history.

### Promotion Gate

Do not switch to local-first immediately after training.

Switch only when the local model is either:

- better than the cloud model on the post-cutoff operator-outcome evaluation
  set, or
- at least 90% of cloud performance on that same evaluation set

In practical terms:

- compare local and cloud against the same real-outcome dataset
- compute the same metric family for both
- require the local model to meet the promotion threshold before changing the
  runtime policy

### Phase 2: Local-First with Cloud Spot Checks

After promotion, change the runtime policy.

- Local becomes the default first decision-maker.
- Cloud is no longer called on every item.
- Cloud is used only when the local model is uncertain or when policy requires
  a stronger check.

The initial uncertainty rule is:

- escalate to cloud when local confidence is below `0.80`

That produces the desired steady state:

- local-first for normal traffic
- cloud spot-checking for uncertain cases
- continued operator-outcome collection for ongoing recalibration

## Practical Summary

The intended loop is:

1. Keep cloud always on while building the disagreement and outcome dataset.
2. Treat cloud as the temporary operational standard for live decisions.
3. Treat real operator outcomes as the strongest long-run correctness signal.
4. Train the local model with prompt tuning first, then LoRA.
5. Evaluate on clean future data only.
6. Switch to local-first only when local is better than cloud or at least 90%
   of cloud on real-outcome evaluation.
7. After the switch, use cloud only for low-confidence local cases below 0.80
   confidence or for explicit policy-required checks.
