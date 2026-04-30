"""Tests for PreferenceRefiner — LLM-driven preference refinement (Loop B)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.eval.preference import (
    Preference,
    PreferenceSource,
    PreferenceStrength,
    PreferenceVersion,
)
from app.eval.preference_refiner import PreferenceRefiner, RefinementProposal
from app.llm.provider import (
    LLMProviderError,
    LLMRequest,
    LLMResponse,
    TokenUsage,
)


def _now() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _active_exit_row_preference() -> Preference:
    return Preference(
        task_id="travel_preferences",
        name="exit_row",
        description="Lutz prefers exit row seating on flights.",
        current=PreferenceVersion(
            rule_text="prefer_exit_row",
            strength=PreferenceStrength.SOFT,
            source=PreferenceSource.COWORK,
            proposed_at=_now() - timedelta(days=30),
            approved_at=_now() - timedelta(days=29),
            approved_by="lutz",
        ),
    )


class _StubProvider:
    """Drop-in Provider that returns canned responses."""

    provider_id = "stub"
    model = "stub-model"

    def __init__(
        self,
        *,
        response: LLMResponse | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._response = response
        self._raise = raise_exc
        self.calls: list[LLMRequest] = []

    def predict(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response


def _refinement_response(
    *,
    rule_text: str,
    reasoning: str = "Generalize for cost-bound exception",
    is_meaningful_change: bool = True,
) -> LLMResponse:
    return LLMResponse(
        output={
            "rule_text": rule_text,
            "reasoning": reasoning,
            "is_meaningful_change": is_meaningful_change,
        },
        raw_text="...",
        usage=TokenUsage(input_tokens=120, output_tokens=30),
        cost_usd=0.001,
        latency_ms=300,
        model_used="stub-model",
        provider_id="stub",
    )


def test_refiner_proposes_new_proposed_version_with_inferred_source() -> None:
    provider = _StubProvider(
        response=_refinement_response(
            rule_text="prefer_exit_row UNLESS price_delta_usd > 30 OR cabin = business"
        )
    )
    refiner = PreferenceRefiner(provider=provider, clock=_now)
    proposal = refiner.propose_refinement(
        active=_active_exit_row_preference(),
        violation_summary=(
            "On 2026-04-15, Lutz booked seat 14B (aisle, non-exit) for "
            "USD 412, $40 less than the cheapest exit-row option."
        ),
    )
    assert isinstance(proposal, RefinementProposal)
    assert proposal.is_meaningful_change is True
    assert proposal.version.strength == PreferenceStrength.PROPOSED
    assert proposal.version.source == PreferenceSource.INFERRED
    assert "price_delta_usd > 30" in proposal.version.rule_text


def test_refiner_passes_violation_into_prompt() -> None:
    provider = _StubProvider(
        response=_refinement_response(rule_text="prefer_exit_row UNLESS X")
    )
    refiner = PreferenceRefiner(provider=provider, clock=_now)
    refiner.propose_refinement(
        active=_active_exit_row_preference(),
        violation_summary="seat 14B; non-exit; $40 cheaper",
    )
    [request] = provider.calls
    assert "seat 14B" in request.prompt
    assert "exit_row" in request.prompt
    assert request.response_schema_name == "PreferenceRefinement"


def test_refiner_includes_prior_violations_in_prompt() -> None:
    provider = _StubProvider(
        response=_refinement_response(rule_text="prefer_exit_row UNLESS Y")
    )
    refiner = PreferenceRefiner(provider=provider, clock=_now)
    refiner.propose_refinement(
        active=_active_exit_row_preference(),
        violation_summary="latest violation",
        prior_violations=[
            "v1: aisle for $20 less",
            "v2: front row for $50 less",
        ],
    )
    [request] = provider.calls
    assert "Prior violations seen (2)" in request.prompt
    assert "v1: aisle for $20 less" in request.prompt


def test_refiner_returns_no_op_proposal_when_change_not_meaningful() -> None:
    provider = _StubProvider(
        response=_refinement_response(
            rule_text="prefer_exit_row",
            reasoning="One-off; user almost always picks exit row.",
            is_meaningful_change=False,
        )
    )
    refiner = PreferenceRefiner(provider=provider, clock=_now)
    proposal = refiner.propose_refinement(
        active=_active_exit_row_preference(),
        violation_summary="single odd booking",
    )
    assert proposal.is_meaningful_change is False
    assert proposal.version.rule_text == "prefer_exit_row"


def test_refiner_swallows_provider_error_into_no_op() -> None:
    provider = _StubProvider(
        raise_exc=LLMProviderError("rate limit", provider_id="stub", model="x"),
    )
    refiner = PreferenceRefiner(provider=provider, clock=_now)
    proposal = refiner.propose_refinement(
        active=_active_exit_row_preference(),
        violation_summary="x",
    )
    assert proposal.is_meaningful_change is False
    assert "refiner unavailable" in proposal.reasoning
    # Returned version is at PROPOSED + INFERRED but with the same rule_text
    # — caller can detect the no-op via is_meaningful_change=False.
    assert proposal.version.strength == PreferenceStrength.PROPOSED
    assert proposal.version.rule_text == "prefer_exit_row"


def test_refiner_proposed_version_carries_reasoning_in_notes() -> None:
    provider = _StubProvider(
        response=_refinement_response(
            rule_text="prefer_exit_row UNLESS price_delta > 30",
            reasoning="Lutz accepts non-exit when delta exceeds $30",
        )
    )
    refiner = PreferenceRefiner(provider=provider, clock=_now)
    proposal = refiner.propose_refinement(
        active=_active_exit_row_preference(),
        violation_summary="violation",
    )
    assert proposal.version.notes is not None
    assert "delta exceeds" in proposal.version.notes


def test_refiner_proposal_can_be_committed_via_propose_revision() -> None:
    """Wiring proof: PreferenceRefiner output flows directly into
    Preference.propose_revision, which deprecates the old version and
    appends history. Approval (setting approved_at) is left to the caller."""

    provider = _StubProvider(
        response=_refinement_response(
            rule_text="prefer_exit_row UNLESS price_delta_usd > 30",
            reasoning="cost-bound refinement",
        )
    )
    refiner = PreferenceRefiner(provider=provider, clock=_now)
    pref = _active_exit_row_preference()

    proposal = refiner.propose_refinement(
        active=pref, violation_summary="violation"
    )
    pref.propose_revision(proposal.version)

    # New current is PROPOSED — not active until human approves.
    assert pref.is_active is False
    assert pref.current.strength == PreferenceStrength.PROPOSED
    assert "price_delta_usd > 30" in pref.current.rule_text
    assert len(pref.history) == 1
    deprecated = pref.history[0]
    assert deprecated.rule_text == "prefer_exit_row"
    assert deprecated.deprecated_at is not None
