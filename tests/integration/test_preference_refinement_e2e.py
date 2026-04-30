"""End-to-end Loop B: cowork → preference proposal → approval → refinement.

Builds the narrative from the design discussion:

  1. Lutz mentions "exit row" in a co-work session → a PROPOSED Preference
     gets recorded.
  2. Slack ask posted asking for approval.
  3. Lutz replies "yes, soft preference" → preference becomes SOFT and active.
  4. Later booking observed: Lutz took non-exit-row aisle for $40 less.
  5. PreferenceRefiner proposes a refined rule (with price_delta cap).
  6. Refined PROPOSED version replaces the SOFT version (via propose_revision);
     a Slack ask posted for the refinement approval.
  7. Lutz approves → refined version flips to SOFT, becomes active.

No real LLM/Slack — everything is stubbed. The test proves the data model
and component composition work end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.eval.preference import (
    Preference,
    PreferenceSource,
    PreferenceStrength,
    PreferenceVersion,
)
from app.eval.preference_refiner import PreferenceRefiner
from app.eval.storage import PreferenceStore
from app.llm.provider import LLMRequest, LLMResponse, TokenUsage


def _now() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


class _StubProvider:
    provider_id = "stub"
    model = "stub-model"

    def __init__(self, *, response: LLMResponse) -> None:
        self._response = response

    def predict(self, _request: LLMRequest) -> LLMResponse:
        return self._response


def _refinement_response(rule_text: str, reasoning: str) -> LLMResponse:
    return LLMResponse(
        output={
            "rule_text": rule_text,
            "reasoning": reasoning,
            "is_meaningful_change": True,
        },
        raw_text="...",
        usage=TokenUsage(input_tokens=100, output_tokens=20),
        cost_usd=0.0,
        latency_ms=200,
        model_used="stub-model",
        provider_id="stub",
    )


@pytest.fixture
def pref_store(tmp_path: Path) -> PreferenceStore:
    return PreferenceStore(root=tmp_path / "eval")


def test_loop_b_full_narrative(pref_store: PreferenceStore) -> None:
    # ── 1. Cowork extracts a PROPOSED preference. ─────────────────────
    proposed = Preference(
        task_id="travel_preferences",
        name="exit_row",
        description="Lutz prefers exit row seating",
        current=PreferenceVersion(
            rule_text="prefer_exit_row",
            strength=PreferenceStrength.PROPOSED,
            source=PreferenceSource.COWORK,
            proposed_at=_now() - timedelta(days=30),
        ),
    )
    pref_store.upsert(proposed)
    assert proposed.is_active is False  # not yet approved

    # ── 2. Lutz approves it as SOFT. ──────────────────────────────────
    soft_version = PreferenceVersion(
        rule_text="prefer_exit_row",
        strength=PreferenceStrength.SOFT,
        source=PreferenceSource.COWORK,
        proposed_at=proposed.current.proposed_at,
        approved_at=_now() - timedelta(days=29),
        approved_by="lutz",
        approval_ask_id="ask-approve-initial",
    )
    proposed.propose_revision(soft_version)
    pref_store.upsert(proposed)

    [restored] = pref_store.load("travel_preferences")
    assert restored.is_active is True
    assert restored.current.strength == PreferenceStrength.SOFT
    assert len(restored.history) == 1  # the original PROPOSED version

    # ── 3. Refiner sees a violation and proposes a refined rule. ──────
    provider = _StubProvider(
        response=_refinement_response(
            rule_text="prefer_exit_row UNLESS price_delta_usd > 30",
            reasoning="Cost-bound refinement",
        )
    )
    refiner = PreferenceRefiner(provider=provider, clock=_now)
    proposal = refiner.propose_refinement(
        active=restored,
        violation_summary=(
            "Lutz booked seat 14B (non-exit aisle) for USD 412, "
            "$40 cheaper than the available exit-row option."
        ),
    )
    assert proposal.is_meaningful_change is True
    assert proposal.version.strength == PreferenceStrength.PROPOSED

    # ── 4. Apply the refinement (replaces current; old goes to history). ─
    restored.propose_revision(proposal.version)
    pref_store.upsert(restored)

    [after_proposal] = pref_store.load("travel_preferences")
    assert after_proposal.is_active is False  # back to PROPOSED, not active
    assert "price_delta_usd > 30" in after_proposal.current.rule_text
    assert len(after_proposal.history) == 2

    # ── 5. Lutz approves the refinement. ──────────────────────────────
    approved_refinement = after_proposal.current.model_copy(
        update={
            "strength": PreferenceStrength.SOFT,
            "approved_at": _now(),
            "approved_by": "lutz",
            "approval_ask_id": "ask-approve-refinement",
        }
    )
    after_proposal.propose_revision(approved_refinement)
    pref_store.upsert(after_proposal)

    # ── 6. End state ──────────────────────────────────────────────────
    [final] = pref_store.load("travel_preferences")
    assert final.is_active is True
    assert final.current.strength == PreferenceStrength.SOFT
    assert "price_delta_usd > 30" in final.current.rule_text
    assert len(final.history) == 3
    # History (oldest → newest):
    #   PROPOSED cowork (deprecated)
    #   SOFT cowork-approved (deprecated)
    #   PROPOSED refinement (deprecated)
    history_strengths = [v.strength for v in final.history]
    assert history_strengths == [
        PreferenceStrength.PROPOSED,
        PreferenceStrength.SOFT,
        PreferenceStrength.PROPOSED,
    ]


def test_travel_task_yaml_loads() -> None:
    """The travel preferences TaskConfig YAML loads without error."""

    from app.runtime.ai_stack.task import TaskConfig

    config = TaskConfig.from_yaml(Path("registry/tasks/travel_preferences.yaml"))
    assert config.task_id == "travel_preferences"
    assert config.reality_observation_window_days == 14
    assert config.metadata.get("loop") == "B"
