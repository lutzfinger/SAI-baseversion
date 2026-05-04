"""Tests for sender validation guard (#6 fail-closed)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.canonical import sender_validation as sv


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    sv.reload()
    yield
    sv.reload()


def _swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict) -> None:
    target = tmp_path / "sender_validation.yaml"
    target.write_text(yaml.safe_dump(body), encoding="utf-8")
    monkeypatch.setattr(sv, "SENDER_VALIDATION_PATH", target)
    sv.reload()


def test_accepts_valid_from_in_allowlist(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {
        "own_addresses": ["op@example.org"],
        "allowed_from_domains": ["example.edu"],
    })
    v = sv.validate_sender(raw_from="student@example.edu")
    assert v.accepted is True
    assert v.normalized_from == "student@example.edu"


def test_rejects_unparseable_from(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {})
    v = sv.validate_sender(raw_from="not an email")
    assert v.accepted is False
    assert v.reason == "from_unparseable"


def test_rejects_operator_own_address_as_forward(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {
        "own_addresses": ["op@example.org"],
        "allowed_from_domains": [],
    })
    v = sv.validate_sender(raw_from="Op <op@example.org>")
    assert v.accepted is False
    assert "forward" in v.reason


def test_rejects_domain_not_allowed(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {
        "own_addresses": [],
        "allowed_from_domains": ["example.edu"],
    })
    v = sv.validate_sender(raw_from="student@bad.com")
    assert v.accepted is False
    assert "from_domain_not_allowed" in v.reason


def test_accepts_subdomain_of_allowed_domain(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {
        "allowed_from_domains": ["example.edu"],
    })
    v = sv.validate_sender(raw_from="student@grad.example.edu")
    assert v.accepted is True


def test_rejects_reply_to_domain_mismatch(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {
        "allowed_from_domains": ["example.edu"],
    })
    v = sv.validate_sender(
        raw_from="student@example.edu",
        raw_reply_to="attacker@bad.com",
    )
    assert v.accepted is False
    assert "reply_to_domain_mismatch" in v.reason


def test_accepts_reply_to_same_domain(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {
        "allowed_from_domains": ["example.edu"],
    })
    v = sv.validate_sender(
        raw_from="student@example.edu",
        raw_reply_to="student@example.edu",
    )
    assert v.accepted is True


def test_extracts_address_from_display_name_format(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"allowed_from_domains": ["example.edu"]})
    v = sv.validate_sender(raw_from='"Real Student" <student@example.edu>')
    assert v.accepted is True
    assert v.normalized_from == "student@example.edu"


def test_rejects_control_chars_in_from(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {})
    v = sv.validate_sender(raw_from="student\x00@example.edu")
    assert v.accepted is False


def test_allowed_from_addresses_bypass_domain_check_with_workflow_id(
    tmp_path, monkeypatch,
):
    """Per-skill allowlist skips the domain check ONLY for the
    calling workflow_id."""
    _swap(tmp_path, monkeypatch, {
        "allowed_from_domains": ["example.edu"],
        "allowed_from_addresses": {
            "test-skill": ["test-fixture@gmail.com"],
        },
    })
    v = sv.validate_sender(
        raw_from="test-fixture@gmail.com",
        workflow_id="test-skill",
    )
    assert v.accepted is True


def test_per_skill_bypass_does_not_leak_across_skills(tmp_path, monkeypatch):
    """e1's test fixture must NOT be accepted when e2 calls."""
    _swap(tmp_path, monkeypatch, {
        "allowed_from_domains": ["example.edu"],
        "allowed_from_addresses": {
            "skill-one": ["fixture@gmail.com"],
        },
    })
    v_one = sv.validate_sender(
        raw_from="fixture@gmail.com", workflow_id="skill-one",
    )
    assert v_one.accepted is True
    v_two = sv.validate_sender(
        raw_from="fixture@gmail.com", workflow_id="skill-two",
    )
    assert v_two.accepted is False
    assert "from_domain_not_allowed" in v_two.reason


def test_no_workflow_id_means_no_bypass(tmp_path, monkeypatch):
    """If workflow_id is omitted, no per-address bypass applies."""
    _swap(tmp_path, monkeypatch, {
        "allowed_from_domains": ["example.edu"],
        "allowed_from_addresses": {
            "any-skill": ["fixture@gmail.com"],
        },
    })
    v = sv.validate_sender(raw_from="fixture@gmail.com")
    assert v.accepted is False
    assert "from_domain_not_allowed" in v.reason


def test_per_skill_allowed_address_still_checks_forward(tmp_path, monkeypatch):
    """An allowlisted address in own_addresses still counts as a
    forward, even with workflow_id."""
    _swap(tmp_path, monkeypatch, {
        "own_addresses": ["fixture@gmail.com"],
        "allowed_from_addresses": {
            "test-skill": ["fixture@gmail.com"],
        },
    })
    v = sv.validate_sender(
        raw_from="fixture@gmail.com", workflow_id="test-skill",
    )
    assert v.accepted is False
    assert "forward" in v.reason


def test_legacy_flat_list_format_raises(tmp_path, monkeypatch):
    """Old flat-list format (pre-2026-05-04) must error loudly so
    the operator notices + migrates."""
    target = tmp_path / "sender_validation.yaml"
    target.write_text(yaml.safe_dump({
        "allowed_from_addresses": ["legacy@example.com"],
    }))
    monkeypatch.setattr(sv, "SENDER_VALIDATION_PATH", target)
    sv.reload()
    with pytest.raises(ValueError, match="must be a dict"):
        sv.validate_sender(raw_from="x@example.edu")


def test_real_runtime_config_allows_e1_test_fixture(monkeypatch):
    """Sanity check: the merged runtime config allows the e1 test
    sender ONLY when workflow_id='cornell-delay-triage' is passed.

    Reads from ~/.sai-runtime/config/sender_validation.yaml — skips
    if that file isn't present (e.g. fresh checkout, no merge run).
    """
    from pathlib import Path
    runtime_cfg = Path.home() / ".sai-runtime" / "config" / "sender_validation.yaml"
    if not runtime_cfg.exists():
        pytest.skip("merged runtime not present — run sai-overlay merge")
    monkeypatch.setattr(sv, "SENDER_VALIDATION_PATH", runtime_cfg)
    sv.reload()
    v_ok = sv.validate_sender(
        raw_from="testcornellstudenttest@gmail.com",
        workflow_id="cornell-delay-triage",
    )
    assert v_ok.accepted is True
    v_strict = sv.validate_sender(
        raw_from="testcornellstudenttest@gmail.com",
    )
    assert v_strict.accepted is False
    assert "from_domain_not_allowed" in v_strict.reason
