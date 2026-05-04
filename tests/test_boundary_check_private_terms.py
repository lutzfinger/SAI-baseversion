"""Tests for the private-terms layer of the boundary linter.

Standard rules already have coverage in tests/runtime/test_boundary_check.py.
This file specifically covers the additional layer that catches operator-
specific terms even in allowlisted files (the gap that let "cherry",
"cornell", "Marketing for Ben" etc. drift into PRINCIPLES.md narratives).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.boundary_check import (
    PRIVATE_TERMS_FILENAME,
    _load_private_terms,
    scan,
)


def _stage_root(tmp_path: Path, *, files: dict[str, str], allowlist: list[str] = None,
                private_terms: list[str] = None) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for relpath, content in files.items():
        p = root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if allowlist is not None:
        (root / "boundary_check_allowlist.txt").write_text(
            "\n".join(allowlist) + "\n", encoding="utf-8",
        )
    if private_terms is not None:
        (root / PRIVATE_TERMS_FILENAME).write_text(
            "\n".join(private_terms) + "\n", encoding="utf-8",
        )
    return root


def _scan(root: Path):
    files = sorted(p for p in root.rglob("*") if p.is_file())
    from scripts.boundary_check import _load_allowlist
    allowlist = _load_allowlist(root)
    private = _load_private_terms(root)
    return scan(root, files, allowlist, private_terms=private)


class TestPrivateTermsLoader:
    def test_no_file_returns_empty(self, tmp_path):
        root = tmp_path / "norepo"
        root.mkdir()
        assert _load_private_terms(root) == []

    def test_loads_patterns_skipping_comments_and_blanks(self, tmp_path):
        root = _stage_root(tmp_path, files={}, private_terms=[
            "# This is a comment",
            "",
            r"\bcherry\b",
            r"\bdinika\b",
            "  # indented comment",
        ])
        patterns = _load_private_terms(root)
        assert len(patterns) == 2
        assert patterns[0].search("cherry picking") is not None
        assert patterns[1].search("Dinika Mahtani") is not None  # case-insensitive

    def test_invalid_regex_warns_and_skips(self, tmp_path, capsys):
        root = _stage_root(tmp_path, files={}, private_terms=[
            r"\bgood\b",
            "[broken",
        ])
        patterns = _load_private_terms(root)
        assert len(patterns) == 1  # broken pattern dropped
        captured = capsys.readouterr()
        assert "invalid regex" in captured.err.lower()


class TestPrivateTermsScanning:
    def test_private_term_in_allowlisted_file_is_caught(self, tmp_path):
        """The exact failure mode we're fixing: PRINCIPLES.md was
        allowlisted, so 'cherry' / 'cornell' / 'keynote' drifted in."""

        root = _stage_root(
            tmp_path,
            files={
                "PRINCIPLES.md": "We use cherry as a private bucket name.\n",
                "innocent.py": "x = 1\n",
            },
            allowlist=["PRINCIPLES.md  # documentation"],
            private_terms=[r"\bcherry\b"],
        )
        violations = _scan(root)
        priv = [v for v in violations if v.rule == "private-term"]
        assert priv, "expected private-term violation in allowlisted file"
        assert any("PRINCIPLES.md" in v.relpath for v in priv)

    def test_private_term_in_normal_file_is_caught(self, tmp_path):
        root = _stage_root(
            tmp_path,
            files={"normal.md": "Reach out to dinika about the fund.\n"},
            private_terms=[r"\bdinika\b"],
        )
        violations = _scan(root)
        priv = [v for v in violations if v.rule == "private-term"]
        assert priv

    def test_no_private_terms_no_violations(self, tmp_path):
        root = _stage_root(
            tmp_path,
            files={"clean.py": "x = 'this is fine'\n"},
            private_terms=[r"\bsecret_word\b"],
        )
        violations = _scan(root)
        priv = [v for v in violations if v.rule == "private-term"]
        assert priv == []

    def test_no_private_terms_file_falls_back_silently(self, tmp_path):
        root = _stage_root(tmp_path, files={"x.py": "ok = True\n"})
        # No private_terms.txt — should run normally without errors.
        violations = _scan(root)
        # Allowed: any non-private-term violations; just verify the
        # scanner didn't crash.
        priv = [v for v in violations if v.rule == "private-term"]
        assert priv == []

    def test_violation_message_includes_match(self, tmp_path):
        root = _stage_root(
            tmp_path,
            files={"narrative.md": "Cornell sent us this last week.\n"},
            private_terms=[r"\bcornell\b"],
        )
        violations = _scan(root)
        priv = [v for v in violations if v.rule == "private-term"]
        assert priv
        assert "Cornell" in priv[0].snippet or "cornell" in priv[0].snippet.lower()
