"""Tests for the LLM registry (#24b).

Code references LLMs by logical role; the registry maps roles to
(vendor, model, tier). Verifies fail-closed semantics for unknown
roles + the env-override escape hatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.llm import registry as llm_registry


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    llm_registry.reload()
    yield
    llm_registry.reload()


def _swap_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict) -> None:
    target = tmp_path / "llm_registry.yaml"
    target.write_text(yaml.safe_dump(body), encoding="utf-8")
    monkeypatch.setattr(llm_registry, "REGISTRY_PATH", target)
    llm_registry.reload()


def test_get_returns_known_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _swap_registry(tmp_path, monkeypatch, {
        "roles": {
            "agent_default": {
                "vendor": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "tier": "low",
            },
        },
    })

    spec = llm_registry.get("agent_default")
    assert spec.vendor == "anthropic"
    assert spec.model == "claude-haiku-4-5-20251001"
    assert spec.tier == "low"


def test_get_unknown_role_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _swap_registry(tmp_path, monkeypatch, {"roles": {}})

    with pytest.raises(llm_registry.UnknownLLMRole, match="not in"):
        llm_registry.get("nonexistent_role")


def test_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _swap_registry(tmp_path, monkeypatch, {
        "roles": {
            "agent_default": {
                "vendor": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "tier": "low",
            },
        },
    })

    out = llm_registry.get_model_for_role(
        "agent_default", env_override="claude-sonnet-4-5-20250929",
    )
    assert out == "claude-sonnet-4-5-20250929"


def test_get_model_for_role_uses_registry_when_no_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _swap_registry(tmp_path, monkeypatch, {
        "roles": {
            "agent_default": {
                "vendor": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "tier": "low",
            },
        },
    })

    out = llm_registry.get_model_for_role("agent_default")
    assert out == "claude-haiku-4-5-20251001"


def test_real_registry_has_required_roles() -> None:
    """The shipped config/llm_registry.yaml MUST define the roles we use."""

    required_roles = {
        "agent_default",
        "agent_high",
        "cascade_local",
        "cascade_cloud",
        "safety_gate_medium",
        "safety_gate_high",
        "cost_dashboard_query",
        "metrics_dashboard_query",
        "cornell_delay_classifier",
        "cornell_delay_reply_drafter",
    }
    actual = set(llm_registry.all_roles().keys())
    missing = required_roles - actual
    assert not missing, f"Missing required roles in llm_registry.yaml: {missing}"


def test_real_registry_specs_are_well_formed() -> None:
    """Every role must have non-empty vendor + model + valid tier."""

    valid_tiers = {"low", "medium", "high"}
    for role, spec in llm_registry.all_roles().items():
        assert spec.vendor, f"role {role} has empty vendor"
        assert spec.model, f"role {role} has empty model"
        assert spec.tier in valid_tiers, f"role {role} tier {spec.tier!r} not in {valid_tiers}"
