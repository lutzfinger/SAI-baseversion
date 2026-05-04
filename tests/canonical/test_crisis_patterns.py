"""Tests for crisis pattern matcher (#6 fail-closed)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.canonical import crisis_patterns as cp


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    cp.reload()
    yield
    cp.reload()


def _swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict) -> None:
    target = tmp_path / "crisis_patterns.yaml"
    target.write_text(yaml.safe_dump(body), encoding="utf-8")
    monkeypatch.setattr(cp, "CRISIS_PATTERNS_PATH", target)
    cp.reload()


def test_matches_simple_pattern(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"patterns": [r"\bharm myself\b"]})
    assert cp.matches_crisis("I want to harm myself") == [r"\bharm myself\b"]
    assert cp.matches_crisis("I want to harm someone else") == []


def test_case_insensitive(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"patterns": [r"\bsuicide\b"]})
    assert cp.matches_crisis("Mentioning Suicide here.") != []


def test_skips_malformed_pattern(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"patterns": ["[unclosed", r"\bvalid\b"]})
    matches = cp.matches_crisis("the valid one")
    assert matches == [r"\bvalid\b"]


def test_skips_blank_and_comment_lines(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"patterns": [
        "",
        "# this is a comment, not a pattern",
        r"\breal\b",
    ]})
    assert cp.matches_crisis("a real word") == [r"\breal\b"]


def test_empty_text_returns_empty(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"patterns": [r"\bsuicide\b"]})
    assert cp.matches_crisis("") == []
    assert cp.matches_crisis(None) == []


def test_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "CRISIS_PATTERNS_PATH", tmp_path / "missing.yaml")
    cp.reload()
    assert cp.matches_crisis("any text") == []
