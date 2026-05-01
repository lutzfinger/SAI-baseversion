"""Eval-centric primitives.

The system's purpose is to grow a high-quality eval dataset where every record's
ground truth comes from REALITY — what the human actually did, what they
explicitly approved in Slack, or what was decided in a co-work session. Tier
predictions are *transient* and never become ground truth, even when every tier
agrees. Cheaper tiers graduate to active runtime when their precision/recall on
ground-truth eval data clears the configured threshold and a human approves.

Public modules:
  - record.py     : EvalRecord, Prediction, ObservedReality, RealityStatus, RealitySource
  - preference.py : Preference, PreferenceVersion, PreferenceStrength, PreferenceSource
  - storage.py    : EvalRecordStore (JSONL), PreferenceStore (YAML)
"""

from __future__ import annotations

from app.eval.ask import Ask, AskKind, AskStatus, AskStore
from app.eval.orchestrator import AskOrchestrator, BucketingFn
from app.eval.preference import (
    Preference,
    PreferenceSource,
    PreferenceStrength,
    PreferenceVersion,
)
from app.eval.preference_refiner import PreferenceRefiner, RefinementProposal
from app.eval.reconciler import (
    RealityReconciler,
    RealityReconciliationRunner,
    ReconciliationOutcome,
    ReconciliationResult,
)
from app.eval.reply_parsers import (
    DEFAULT_SKIP_KEYWORDS,
    ReplyParser,
    option_matching_parser,
    permissive_text_parser,
)
from app.eval.record import (
    EvalRecord,
    ObservedReality,
    Prediction,
    RealitySource,
    RealityStatus,
)
from app.eval.storage import EvalRecordStore, PreferenceStore

__all__ = [
    "Ask",
    "AskKind",
    "AskOrchestrator",
    "AskStatus",
    "AskStore",
    "BucketingFn",
    "EvalRecord",
    "EvalRecordStore",
    "ObservedReality",
    "Prediction",
    "Preference",
    "PreferenceRefiner",
    "PreferenceSource",
    "PreferenceStore",
    "PreferenceStrength",
    "PreferenceVersion",
    "RefinementProposal",
    "RealityReconciler",
    "RealityReconciliationRunner",
    "RealitySource",
    "RealityStatus",
    "ReconciliationOutcome",
    "ReconciliationResult",
    "ReplyParser",
    "DEFAULT_SKIP_KEYWORDS",
    "option_matching_parser",
    "permissive_text_parser",
]
