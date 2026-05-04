"""Tests for the second-opinion gate tier (#16f / #10 / design doc).

Uses a stub Provider that returns canned JSON. No live LLM calls.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from app.runtime.ai_stack.tiers.second_opinion import (
    SecondOpinionInput,
    SecondOpinionTier,
    SecondOpinionVerdict,
    _coerce_send_back_if_invalid,
    build_retry_prompt,
)
from app.shared import prompt_loader


class StubProvider:
    def __init__(self, response: dict | Exception):
        self.response = response
        self.calls: list[str] = []

    def predict_json(self, prompt: str) -> dict:
        self.calls.append(prompt)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def setup_prompt(tmp_path: pytest.TempPathFactory, monkeypatch):
    """Write a hash-locked criteria_prompt under tmp_path/prompts/."""
    body = "---\nprompt_id: test_gate\nversion: '1'\n---\nReject anything mentioning bombs.\n"
    prompts_dir = tmp_path / "prompts"
    safety_dir = prompts_dir / "safety"
    safety_dir.mkdir(parents=True)
    target = safety_dir / "test_gate.md"
    target.write_text(body, encoding="utf-8")
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    locks_path = prompts_dir / "prompt-locks.yaml"
    locks_path.write_text(yaml.safe_dump({
        "prompts": {"safety/test_gate.md": sha},
    }))
    monkeypatch.setattr(prompt_loader, "REPO_ROOT", tmp_path)
    prompt_loader.reload_locks()
    return tmp_path


def _payload(**overrides):
    base = dict(
        workflow_id="test-wf",
        purpose="Test workflow that does nothing harmful.",
        criteria_prompt_relpath="safety/test_gate.md",
        proposed_input={"text": "harmless"},
        proposed_output={"reply": "all good"},
        producer_tier_kind="cloud_llm",
        prior_attempts=0,
    )
    base.update(overrides)
    return SecondOpinionInput(**base)


def test_allow_verdict_passes_through(setup_prompt):
    provider = StubProvider({
        "verdict": "allow", "reasoning": "ok", "triggers": [],
        "confidence": 0.95, "gate_prompt_sha256": "",
    })
    tier = SecondOpinionTier(tier_id="gate1", provider=provider)
    v = tier.evaluate(_payload())
    assert v.verdict == "allow"
    assert v.confidence == 0.95


def test_refuse_verdict_passes_through(setup_prompt):
    provider = StubProvider({
        "verdict": "refuse", "reasoning": "mentions bomb",
        "triggers": ["unsafe"], "confidence": 0.99,
    })
    tier = SecondOpinionTier(tier_id="gate1", provider=provider)
    v = tier.evaluate(_payload())
    assert v.verdict == "refuse"


def test_send_back_passes_through_when_first_attempt_and_llm_producer(setup_prompt):
    provider = StubProvider({
        "verdict": "send_back", "reasoning": "tone too casual",
        "triggers": ["tone"], "confidence": 0.7,
    })
    tier = SecondOpinionTier(tier_id="gate1", provider=provider)
    v = tier.evaluate(_payload(prior_attempts=0, producer_tier_kind="cloud_llm"))
    assert v.verdict == "send_back"
    assert "tone too casual" in v.reasoning


def test_send_back_coerced_to_escalate_on_second_attempt(setup_prompt):
    provider = StubProvider({
        "verdict": "send_back", "reasoning": "still off",
        "triggers": ["tone"], "confidence": 0.5,
    })
    tier = SecondOpinionTier(tier_id="gate1", provider=provider)
    v = tier.evaluate(_payload(prior_attempts=1, producer_tier_kind="cloud_llm"))
    assert v.verdict == "escalate"
    assert "send_back coerced" in v.reasoning
    assert "single_shot_rule" in v.reasoning


def test_send_back_coerced_to_escalate_when_producer_is_deterministic(setup_prompt):
    provider = StubProvider({
        "verdict": "send_back", "reasoning": "would prefer different",
        "triggers": [], "confidence": 0.6,
    })
    tier = SecondOpinionTier(tier_id="gate1", provider=provider)
    v = tier.evaluate(_payload(producer_tier_kind="rules"))
    assert v.verdict == "escalate"
    assert "deterministic_producer" in v.reasoning


def test_provider_error_yields_escalate(setup_prompt):
    provider = StubProvider(RuntimeError("boom"))
    tier = SecondOpinionTier(tier_id="gate1", provider=provider)
    v = tier.evaluate(_payload())
    assert v.verdict == "escalate"
    assert "gate_provider_error" in v.reasoning


def test_malformed_provider_output_yields_escalate(setup_prompt):
    provider = StubProvider({"this_is_not_a_valid_verdict": True})
    tier = SecondOpinionTier(tier_id="gate1", provider=provider)
    v = tier.evaluate(_payload())
    assert v.verdict == "escalate"
    assert "gate_output_malformed" in v.reasoning


def test_hash_mismatch_yields_escalate(setup_prompt, monkeypatch):
    # Tamper with the lock file to cause mismatch.
    locks_path = setup_prompt / "prompts" / "prompt-locks.yaml"
    locks_path.write_text(yaml.safe_dump({
        "prompts": {"safety/test_gate.md": "deadbeef"},
    }))
    prompt_loader.reload_locks()
    provider = StubProvider({
        "verdict": "allow", "reasoning": "ok", "triggers": [],
        "confidence": 0.9,
    })
    tier = SecondOpinionTier(tier_id="gate1", provider=provider)
    v = tier.evaluate(_payload())
    assert v.verdict == "escalate"
    assert "hash_verification" in v.reasoning


def test_coerce_send_back_helper():
    # Valid send_back: first attempt, LLM producer.
    assert _coerce_send_back_if_invalid(
        "send_back", producer_tier_kind="cloud_llm", prior_attempts=0,
    ) is None
    # Invalid: second attempt.
    assert _coerce_send_back_if_invalid(
        "send_back", producer_tier_kind="cloud_llm", prior_attempts=1,
    ) == "single_shot_rule"
    # Invalid: rules producer.
    assert _coerce_send_back_if_invalid(
        "send_back", producer_tier_kind="rules", prior_attempts=0,
    ) == "deterministic_producer_no_retry_meaningful"
    # Not send_back: no coercion.
    assert _coerce_send_back_if_invalid(
        "allow", producer_tier_kind="rules", prior_attempts=5,
    ) is None


def test_build_retry_prompt_concatenates_correctly():
    out = build_retry_prompt(
        original_prompt="Classify the email.",
        original_output="exception",
        gate_reasoning="Should be no_exception — work travel is routine.",
    )
    assert "Classify the email." in out
    assert "── Previous attempt ──" in out
    assert "exception" in out
    assert "── Reviewer note ──" in out
    assert "work travel is routine" in out
    assert "Please retry" in out


def test_reasoning_truncated_to_2000_chars(setup_prompt):
    huge = "x" * 5000
    provider = StubProvider({
        "verdict": "allow", "reasoning": huge, "triggers": [],
        "confidence": 0.9,
    })
    tier = SecondOpinionTier(tier_id="gate1", provider=provider)
    v = tier.evaluate(_payload())
    assert len(v.reasoning) == 2000


def test_send_back_includes_critique_in_payload_for_retry(setup_prompt):
    """Smoke check: gate's reasoning is what becomes the retry critique."""
    provider = StubProvider({
        "verdict": "send_back",
        "reasoning": "Reply is too curt; expand the policy quote.",
        "triggers": ["tone"], "confidence": 0.65,
    })
    tier = SecondOpinionTier(tier_id="gate1", provider=provider)
    v = tier.evaluate(_payload())
    retry = build_retry_prompt(
        original_prompt="Draft a reply.",
        original_output="No.",
        gate_reasoning=v.reasoning,
    )
    assert "expand the policy quote" in retry
