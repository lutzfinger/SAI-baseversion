"""Eval dataset shapes — the three datasets per PRINCIPLES.md §16a (revised 2026-05-01).

The eval system has exactly three datasets, each with a defined consumer
and a defined shape:

  A) ``CanaryRow``           — synthetic test, one per rules-tier rule.
                               Lives in ``eval/canaries.jsonl``.
                               Generated, not curated. Hard-fail on miss.
  B) ``EdgeCaseRow``         — real email the LLM had to reason about.
                               Lives in ``eval/edge_cases.jsonl``.
                               Curated by operator. Capped at SOFT_CAP (50).
                               Probabilistic (P/R/F1). Shrinks as edges
                               get promoted to rules.
  C) ``DisagreementRow``     — raw local-vs-cloud disagreement awaiting
                               batch surfacing. Lives in
                               ``eval/disagreement_queue.jsonl``.
                               Drained when batch ask resolves.

That's it. Three datasets, no auxiliary queues. When a curation pass
surfaces a row where the rules tier fires confidently but conflicts with
an operator label, the resolution is *immediate* — the operator either
edits the rule (Loop 4) or discards the row. Persistent "rule review
backlog" files are explicitly disallowed; they accumulate stale
decisions and add no value.

Promotion rule (PRINCIPLES.md §16a, 2026-05-01 revision):
  When a Loop 3 / Loop 4 resolution produces a new fixed rule, every
  EdgeCaseRow that the new rule covers at >= production confidence is
  removed from B. The rule's CanaryRow takes its place in A. Net
  direction: B shrinks as the rules tier absorbs more cases.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.workers.email_models import (
    EmailMessage,
    Level1Classification,
    Level2Intent,
)

# Soft cap on B (edge cases). Loop 3 / Loop 4 must evict redundant rows
# when adding new ones if B is at cap. The aim is rules-handle-everything;
# LLM is the residual.
EDGE_CASE_SOFT_CAP: int = 50

# Disagreement queue threshold — when at or above, the curator surfaces
# a batch ask. Per operator (2026-05-01).
DISAGREEMENT_BATCH_THRESHOLD: int = 50


# ─── A. Canaries ──────────────────────────────────────────────────────


class CanaryRow(BaseModel):
    """One synthetic test for one rules-tier rule entry.

    Generated deterministically from the rules-tier config (e.g. the
    keyword-classify prompt frontmatter). The set is reproducible
    across runs and stays in sync as rules are added or removed.

    Regression test: feed ``synthetic_email`` through the rules tier;
    assert ``prediction.confidence >= min_confidence`` AND
    ``prediction.output.level1_classification == expected_level1_classification``.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    """Stable id of the form ``<rule_kind>::<rule_value>::<expected_l1>``."""

    rule_kind: Literal[
        "sender_email",
        "sender_domain",
        "sender_domain_direct_address",
        "skip_first_email_sender",
        "skip_first_email_domain",
    ]
    """Which family of rule this canary covers."""

    rule_value: str
    """The literal sender / domain / etc. the rule keys on."""

    expected_level1_classification: Level1Classification
    """The L1 the rule should produce. For skip rules this is the
    fallback ('other'), which the runtime treats as "no L1 label
    applied" — see ``expected_action`` for the human-readable
    distinction."""

    expected_action: Literal["apply_l1_label", "skip_l1_tagging"] = "apply_l1_label"
    """What the runtime should DO with this canary's prediction.

    - ``apply_l1_label``: rule fires a real bucket → Gmail gets the
      ``L1/<bucket>`` label. The default for sender_email,
      sender_domain, sender_domain_direct_address rules.
    - ``skip_l1_tagging``: rule fires the ``other`` fallback → Gmail
      gets NO L1 label. The contract for skip-first-email rules.

    Both are real, distinct production behaviours. The canary tests
    that the rule produces the right behaviour, not just the right
    string in the L1 field.
    """

    min_confidence: float = 0.85
    """Minimum confidence the rule must produce. Default = production threshold."""

    generated_at: datetime
    """When this canary was emitted by the generator."""

    synthetic_email: EmailMessage
    """Minimal synthetic input designed to fire the rule."""


# ─── B. Edge cases (the LLM regression set) ────────────────────────────


class EdgeCaseRow(BaseModel):
    """One real email that exercised the LLM tier (rules abstained or
    fired below production confidence) and got an operator-confirmed L1.

    Sources: operator additions via Loop 4, and curator picks from
    Loop 2 batch resolutions.

    Constraint: a row only belongs in B if the rules tier does NOT
    fire at >= 0.85 on it. If a rule edit promotes coverage, this row
    must be removed from B.
    """

    model_config = ConfigDict(extra="forbid")

    edge_case_id: str
    """Stable id; convention: ``edge::<message_id>::<expected_l1>``."""

    captured_at: datetime
    source: Literal[
        "operator_loop4",            # operator-driven add via #sai-eval
        "loop3_batch_resolution",    # came back from a Loop 2 batch ask
        "legacy_fixture_curated",    # imported from the pre-system fixture
    ]
    requested_by: Optional[str] = None
    correction_reason: Optional[str] = None

    # Email content (denormalised so the regression set is self-contained)
    message_id: str
    thread_id: Optional[str] = None
    from_email: str
    from_name: Optional[str] = None
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str
    snippet: str
    body_excerpt: str = ""
    body: Optional[str] = None
    received_at: Optional[datetime] = None

    # Operator's verdict — the ground truth
    expected_level1_classification: Level1Classification
    expected_level2_intent: Level2Intent = "others"
    raw_level1_label: str
    raw_level2_label: str = "Others"


# ─── C. Disagreements queue (raw signal awaiting batch surfacing) ──────


class DisagreementRow(BaseModel):
    """One local-vs-cloud disagreement captured at runtime, awaiting
    operator resolution via a Loop 2 batch ask.

    Drained: when the curator picks rows for a batch and the operator
    resolves them, those rows are deleted from this queue. Resolved
    rows that become EdgeCaseRow (or trigger a rule edit) are persisted
    in B / A respectively.
    """

    model_config = ConfigDict(extra="forbid")

    disagreement_id: str
    """Stable id; convention: ``disagree::<message_id>::<captured_at>``."""

    captured_at: datetime
    message_id: str
    thread_id: Optional[str] = None

    # The full message (so the queue row is self-contained — comparison
    # files may have been rotated by the time the operator sees it)
    message: EmailMessage

    # The two predictions and the runtime tiebreaker (cloud)
    local_prediction_l1: Optional[Level1Classification] = None
    local_prediction_reason: Optional[str] = None
    local_prediction_confidence: Optional[float] = None

    cloud_prediction_l1: Level1Classification
    cloud_prediction_reason: Optional[str] = None
    cloud_prediction_confidence: Optional[float] = None

    runtime_winner: Literal["cloud"] = "cloud"
    """Cloud wins runtime per cascade design. Operator verdict (when it
    arrives via Loop 2) is the actual ground truth."""

    # Optional signal from the rules tier (usually abstain or low-conf)
    rules_prediction_l1: Optional[Level1Classification] = None
    rules_prediction_confidence: Optional[float] = None

    surfaced_in_batch_id: Optional[str] = None
    """Set when the curator includes this row in a batch ask. Cleared
    only on resolution (delete from queue)."""


# ─── D. Workflow case (generic — for any workflow's regression set) ───
#
# Every workflow under §33 ships a workflow_regression.jsonl. The case
# shape is uniform — input, expected outcome (one of a workflow-defined
# set), expected tool calls, expected message constraints. Specific
# workflows (e.g. sai-eval) can subclass to add fields if needed.


class WorkflowCase(BaseModel):
    """One workflow regression test case.

    Generic across workflows. Covers the common pattern: feed
    ``input_text`` through the workflow's entry point, assert the
    workflow's outcome matches ``expected_outcome``, optionally check
    tool calls and message content.

    Subclasses may add workflow-specific fields. The original
    sai-eval-specific fields (expected_proposal_kind, etc.) live as
    optional fields here so the SlackEvalCase alias works without a
    schema change.
    """

    model_config = ConfigDict(extra="ignore")

    case_id: str
    description: str = ""
    input_text: str
    expected_outcome: str
    """Workflow-defined outcome value(s). Examples for sai-eval:
    refused / proposed_classifier_rule / proposed_llm_example /
    refused_label_missing / either_propose_or_clarify."""

    tier_under_test: str = "llm_agent"

    # Tool-call assertions (used when the workflow has an agent tier)
    expected_tool_calls: list[str] = Field(default_factory=list)
    expected_tool_calls_subset: list[str] = Field(default_factory=list)
    tool_calls_must_include_when_thread_found: list[str] = Field(
        default_factory=list,
    )

    # Outcome details
    expected_proposal_kind: Optional[str] = None
    expected_message_must_contain: list[str] = Field(default_factory=list)
    expected_message_must_contain_any_of: list[str] = Field(default_factory=list)
    expected_message_must_not_contain: list[str] = Field(default_factory=list)


# ─── EvalDataset subclasses ───────────────────────────────────────────
#
# Per PRINCIPLES.md §16a (revised 2026-05-03): all four are instances
# of ``EvalDataset``. Each binds the abstraction to a specific case
# model + target_kind + cap policy.


from app.eval.dataset_base import EvalDataset


class CanaryDataset(EvalDataset):
    """Eval dataset for the rules tier — one canary per rule.

    No cap (1:1 with rules; the rules config is the source of truth).
    Hard-fail: any miss = rollback.
    Generated deterministically by ``scripts/generate_classifier_canaries.py``.
    """

    case_model = CanaryRow
    dataset_kind = "canaries"
    target_kind = "rules"
    default_cap = None
    default_fail_mode = "hard_fail"


class EdgeCaseDataset(EvalDataset):
    """Eval dataset for the LLM tier — operator-curated real cases.

    Soft-cap at EDGE_CASE_SOFT_CAP (default 50) per #16a — forces
    curation discipline. When at cap, ``append`` evicts the most-
    redundant existing row; if a True-North companion is set via
    ``on_evict``, the evicted row is archived there (#16h).
    soft_fail: caller compares P/R drop against threshold.
    """

    case_model = EdgeCaseRow
    dataset_kind = "edge_cases"
    target_kind = "llm"
    default_cap = EDGE_CASE_SOFT_CAP
    default_fail_mode = "soft_fail"


class DisagreementDataset(EvalDataset):
    """Local-vs-cloud disagreement queue. Drained by the Loop 2 batch
    surfacing (resolved rows are deleted; promoted witnesses are
    appended to EdgeCaseDataset OR True-North).
    """

    case_model = DisagreementRow
    dataset_kind = "disagreement_queue"
    target_kind = "llm"
    default_cap = None  # bounded by Loop 2 cadence, not size
    default_fail_mode = "soft_fail"


class WorkflowDataset(EvalDataset):
    """Eval dataset for a workflow ITSELF (not its tiers).

    Catches drift in the workflow's plumbing — system prompt, regex
    parsers, tool wiring, regression hook. Per #16d every workflow
    has one. Hard-fail: any case failure blocks apply.

    Subclasses can override ``case_model`` to use a workflow-specific
    case shape; the default ``WorkflowCase`` covers the common pattern.
    """

    case_model = WorkflowCase
    dataset_kind = "workflow"
    target_kind = "workflow"
    default_cap = None
    default_fail_mode = "hard_fail"


class TrueNorthDataset(EvalDataset):
    """Uncapped append-only historical record (#16h).

    Every operator-approved case the workflow has ever seen, plus rows
    promoted from EdgeCaseDataset when soft-cap evicts. Run occasionally
    (weekly cron OR manual) for full-fidelity completion checks; doesn't
    gate every change.

    Same case shape as EdgeCaseRow by default. Per-workflow override
    via subclassing.
    """

    case_model = EdgeCaseRow
    dataset_kind = "true_north"
    target_kind = "llm"
    default_cap = None  # uncapped — that's the point
    default_fail_mode = "soft_fail"

    def append_archived(
        self, evicted_case: EdgeCaseRow,
    ) -> None:
        """Convenience: the on_evict callback EdgeCaseDataset uses
        when promoting from working → True-North."""

        # Could enrich with archived_from_working_at here in the future;
        # for v1 just append.
        self.append(evicted_case)


# ─── back-compat alias ────────────────────────────────────────────────
# slack_eval_regression.py historically used `SlackEvalCase`; the
# generic case model is now WorkflowCase. Keep the alias so existing
# imports continue to work.

SlackEvalCase = WorkflowCase

