"""PreferenceRefiner — LLM-driven proposal of refined preferences.

The system observes a *violation* (an active preference that didn't apply
to a real-world action — e.g. user took a non-exit-row seat for $40 less).
The PreferenceRefiner asks an LLM Provider to propose a more nuanced rule
that accommodates the observed exception, then returns a new
PreferenceVersion(strength=PROPOSED, source=INFERRED). The caller is
responsible for posting the proposal as a Slack ask; the human decides
whether to apply the refinement.

Refiner NEVER edits a preference directly. Every refinement requires
human approval — the new version stays at strength=PROPOSED until the
approval Ask is answered (handled by step 5/6 + an approval applier
that's task-specific).

The LLM prompt is opinionated about output format:
  - rule_text: the new conditional, in YAML/DSL format
  - reasoning: a short justification (≤ 30 words) for the change
  - is_meaningful_change: false if the refiner thinks the violation was
    a one-off; true if it really is a generalizable refinement
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.eval.preference import (
    Preference,
    PreferenceSource,
    PreferenceStrength,
    PreferenceVersion,
)
from app.llm.provider import LLMProviderError, LLMRequest, Provider

REFINER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rule_text": {
            "type": "string",
            "description": (
                "The refined rule, in YAML/DSL form. Must accommodate the "
                "observed exception while keeping the original spirit."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": "≤ 30 words justifying the refinement.",
        },
        "is_meaningful_change": {
            "type": "boolean",
            "description": (
                "false if the violation looks like a one-off and no rule "
                "change is warranted; true if the rule should be refined."
            ),
        },
    },
    "required": ["rule_text", "reasoning", "is_meaningful_change"],
}


class RefinementProposal:
    """The refiner's proposed update plus its decision flag."""

    __slots__ = ("version", "is_meaningful_change", "reasoning")

    def __init__(
        self,
        *,
        version: PreferenceVersion,
        is_meaningful_change: bool,
        reasoning: str,
    ) -> None:
        self.version = version
        self.is_meaningful_change = is_meaningful_change
        self.reasoning = reasoning


class PreferenceRefiner:
    """Use an LLM Provider to propose a refined PreferenceVersion."""

    def __init__(
        self,
        *,
        provider: Provider,
        clock: Any = None,
    ) -> None:
        self.provider = provider
        self._clock = clock or (lambda: datetime.now(UTC))

    def propose_refinement(
        self,
        *,
        active: Preference,
        violation_summary: str,
        prior_violations: list[str] | None = None,
    ) -> RefinementProposal:
        """Ask the LLM to propose a refined rule. Returns a PROPOSED version.

        The caller is expected to surface this proposal to a human (via
        SlackAskUI) and apply it only on approval.
        """

        prompt = _render_prompt(
            active=active,
            violation_summary=violation_summary,
            prior_violations=prior_violations or [],
        )
        request = LLMRequest(
            prompt=prompt,
            response_schema=REFINER_RESPONSE_SCHEMA,
            response_schema_name="PreferenceRefinement",
            max_output_tokens=512,
            temperature=0.2,
        )
        try:
            response = self.provider.predict(request)
        except LLMProviderError as exc:
            # On provider failure, return a non-meaningful no-op proposal
            # rather than raising — the caller decides whether to retry.
            return RefinementProposal(
                version=_unchanged_version(active, now=self._clock()),
                is_meaningful_change=False,
                reasoning=f"refiner unavailable: {exc}",
            )

        output = response.output
        rule_text = str(output.get("rule_text") or active.current.rule_text)
        reasoning = str(output.get("reasoning") or "")
        is_meaningful = bool(output.get("is_meaningful_change", False))

        version = PreferenceVersion(
            rule_text=rule_text,
            strength=PreferenceStrength.PROPOSED,
            source=PreferenceSource.INFERRED,
            proposed_at=self._clock(),
            notes=reasoning[:400] if reasoning else None,
        )
        return RefinementProposal(
            version=version,
            is_meaningful_change=is_meaningful,
            reasoning=reasoning,
        )


def _render_prompt(
    *,
    active: Preference,
    violation_summary: str,
    prior_violations: list[str],
) -> str:
    parts = [
        "You are refining a user's preference based on observed reality.",
        "",
        f"Preference name: {active.name}",
        f"Description: {active.description}",
        f"Current rule (strength={active.current.strength.value}):",
        f"  {active.current.rule_text}",
        "",
        "Observed violation (the user did NOT follow the rule this time):",
        f"  {violation_summary}",
    ]
    if prior_violations:
        parts.extend(
            [
                "",
                f"Prior violations seen ({len(prior_violations)}):",
                *(f"  - {v}" for v in prior_violations[:5]),
            ]
        )
    parts.extend(
        [
            "",
            "Propose a refined rule that accommodates the new exception",
            "while keeping the original spirit of the preference. If the",
            "violation looks like a one-off (no clear pattern), set",
            "is_meaningful_change=false and return the current rule unchanged.",
        ]
    )
    return "\n".join(parts)


def _unchanged_version(active: Preference, *, now: datetime) -> PreferenceVersion:
    return PreferenceVersion(
        rule_text=active.current.rule_text,
        strength=PreferenceStrength.PROPOSED,
        source=PreferenceSource.INFERRED,
        proposed_at=now,
        notes="refiner returned no meaningful change",
    )
