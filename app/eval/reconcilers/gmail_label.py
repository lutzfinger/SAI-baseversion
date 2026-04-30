"""GmailLabelReconciler — reality observer for email-classification tasks.

For each pending email-classification record, look at the thread's current
labels in Gmail. If the user has re-tagged with an L1/* label different from
what we applied, that's the ground truth (HUMAN_LABEL). If the thread has
been archived without further label changes, that's HUMAN_ACTION (often
ambiguous).

Public ships the protocol-conforming class. The Gmail-specific label-fetch
function is injected at construction (it can be `GmailLabelConnector` from
private overlays, a mock, or whatever observation surface fits). This keeps
the public starter shippable without OAuth tokens or Gmail credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.connectors.gmail_labels import is_taxonomy_classification_label
from app.eval.reconciler import (
    ReconciliationOutcome,
    ReconciliationResult,
)
from app.eval.record import EvalRecord, ObservedReality, RealitySource

ThreadLabelsFn = Callable[[str], set[str] | None]
"""Given a Gmail thread_id, return the current set of label names, or None
if the thread is missing / not accessible. The caller is responsible for
all Gmail API plumbing; the reconciler just diffs."""


class GmailLabelReconciler:
    """Observe reality for email tasks via the user's Gmail labels.

    Pluggable observation: `thread_labels_fn` returns the current label set
    for a thread. The reconciler:
      1. Reads the current labels.
      2. Looks for taxonomy labels (L1/*, L2/*) that differ from what
         `active_decision` applied.
      3. If found, emits a HUMAN_LABEL ObservedReality.
      4. Otherwise STILL_PENDING — wait for the next reconciliation cycle.
    """

    def __init__(
        self,
        *,
        task_id: str,
        thread_labels_fn: ThreadLabelsFn,
        applied_label_extractor: Callable[[dict[str, Any]], set[str]],
    ) -> None:
        self.task_id = task_id
        self.thread_labels_fn = thread_labels_fn
        self.applied_label_extractor = applied_label_extractor

    def reconcile_one(
        self, record: EvalRecord, *, now: datetime | None = None
    ) -> ReconciliationResult:
        # The input_id for email tasks is conventionally the thread_id (or
        # message_id within a thread). The runtime owns the convention; the
        # reconciler just uses it as the lookup key.
        thread_id = record.input_id
        try:
            current_labels = self.thread_labels_fn(thread_id)
        except Exception:  # pragma: no cover - any failure → still pending
            return ReconciliationResult(outcome=ReconciliationOutcome.STILL_PENDING)
        if current_labels is None:
            return ReconciliationResult(outcome=ReconciliationOutcome.STILL_PENDING)

        applied_labels = self.applied_label_extractor(record.active_decision)
        applied_taxonomy = {
            label for label in applied_labels if is_taxonomy_classification_label(label)
        }
        current_taxonomy = {
            label for label in current_labels if is_taxonomy_classification_label(label)
        }

        if not current_taxonomy:
            # User cleared taxonomy labels — that's a meaningful signal but
            # ambiguous on its own. Leave still pending; a separate
            # archive/reply reconciler can capture HUMAN_ACTION.
            return ReconciliationResult(outcome=ReconciliationOutcome.STILL_PENDING)

        if current_taxonomy == applied_taxonomy:
            # User accepted what we applied. That's not new ground truth on
            # its own (could be inertia), but it IS a passive confirmation.
            return ReconciliationResult(
                outcome=ReconciliationOutcome.OBSERVED,
                reality=ObservedReality(
                    label={"labels": sorted(current_taxonomy)},
                    source=RealitySource.HUMAN_ACTION,
                    observed_at=now or datetime.now(UTC),
                    notes="user did not change applied labels (passive confirmation)",
                    raw_signal={
                        "applied_labels": sorted(applied_taxonomy),
                        "current_labels": sorted(current_taxonomy),
                    },
                ),
            )

        # User actively re-labeled. That's HUMAN_LABEL — the strongest signal.
        return ReconciliationResult(
            outcome=ReconciliationOutcome.OBSERVED,
            reality=ObservedReality(
                label={"labels": sorted(current_taxonomy)},
                source=RealitySource.HUMAN_LABEL,
                observed_at=now or datetime.now(UTC),
                notes="user re-tagged the thread differently from active decision",
                raw_signal={
                    "applied_labels": sorted(applied_taxonomy),
                    "current_labels": sorted(current_taxonomy),
                },
            ),
        )
