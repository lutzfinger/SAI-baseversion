"""Phase 2 tests: boundary linter for the public repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.boundary_check import (
    ALLOWLIST_FILENAME,
    Violation,
    main,
    scan_line,
)


# ---------- per-line scan ----------


def test_scan_line_clean() -> None:
    assert scan_line("ok.py", 1, "x = 1") == []


def test_scan_line_email_placeholder_ok() -> None:
    assert scan_line("ok.py", 1, "user@example.com") == []
    assert scan_line("ok.py", 1, "user@example.org") == []
    assert scan_line("ok.py", 1, "user@test") == []


def test_scan_line_email_real_domain_flagged() -> None:
    violations = scan_line("ok.py", 1, "lutz@gmail.com")
    assert len(violations) == 1
    assert violations[0].rule == "email-non-placeholder"


def test_scan_line_personal_string_flagged() -> None:
    for s in ("lutzfinger", "lfinger", "Lutz_Dev", "LUTZFINGER"):
        violations = scan_line("ok.py", 1, f"foo {s} bar")
        assert any(v.rule == "personal-string" for v in violations), s


def test_scan_line_users_path_flagged() -> None:
    violations = scan_line("ok.py", 1, "/Users/alice/Documents")
    assert any(v.rule == "users-path" for v in violations)


def test_scan_line_users_example_path_ok() -> None:
    assert scan_line("ok.py", 1, "/Users/example/foo") == []


def test_scan_line_slack_channel_flagged() -> None:
    violations = scan_line("ok.py", 1, "post to #notes-and-todos today")
    assert any(v.rule == "slack-channel-non-placeholder" for v in violations)


def test_scan_line_slack_placeholder_ok() -> None:
    for ch in ("#general", "#example", "#test-channel"):
        assert scan_line("ok.py", 1, f"post to {ch}") == []


def test_scan_line_css_hex_color_not_flagged() -> None:
    """The earlier regex caught CSS hex colors like #d0d7de and #444 — must not."""
    for color in ("#fff", "#ffff", "#abcdef", "#abcdef12", "#444", "#d0d7de"):
        result = scan_line("style.css", 1, f"color: {color};")
        assert all(v.rule != "slack-channel-non-placeholder" for v in result), color


def test_scan_line_phone_flagged() -> None:
    for s in ("+1 415-555-1234", "(212) 555-9876", "415.555.1234"):
        violations = scan_line("ok.py", 1, f"call me at {s}")
        # Some loose phones may also be flagged as multiple things; just check phone is in there.
        assert any(v.rule == "phone-number" for v in violations), s


def test_scan_line_phone_placeholder_ok() -> None:
    """5555555555, 1234567890, 0000000000 are placeholders, not real numbers."""
    for s in ("555-555-5555", "123-456-7890", "000-000-0000"):
        result = scan_line("ok.py", 1, f"call {s}")
        assert all(v.rule != "phone-number" for v in result), s


def test_scan_line_secret_scheme_flagged() -> None:
    for s in ("op://vault/item", "keychain://sai/openai_key"):
        violations = scan_line("ok.py", 1, s)
        assert any(v.rule == "secret-scheme-reference" for v in violations), s


def test_scan_line_aggregates_multiple_rules() -> None:
    line = "lutz@gmail.com lives at /Users/lutz and uses lutzfinger handle"
    violations = scan_line("ok.py", 1, line)
    rule_types = {v.rule for v in violations}
    assert rule_types == {"email-non-placeholder", "users-path", "personal-string"}


# ---------- end-to-end run via main() ----------


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "clean.py").write_text(
        '"""User: alice@example.com lives at /Users/example/x"""\n'
    )
    return tmp_path


def test_main_clean_repo_returns_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _make_repo(tmp_path)
    rc = main(["--root", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "boundary check ok" in out


def test_main_dirty_repo_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src" / "leak.py").write_text(
        "secret = 'op://vault/key'\n"
        "user = 'lutz@gmail.com'\n"
    )
    rc = main(["--root", str(repo)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "boundary check FAILED" in err
    assert "secret-scheme-reference" in err
    assert "email-non-placeholder" in err


def test_main_allowlist_exempts_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src" / "leak.py").write_text("user = 'lutz@gmail.com'\n")
    (repo / ALLOWLIST_FILENAME).write_text(
        "src/leak.py\n# this fixture is intentionally dirty for the test suite\n"
    )
    rc = main(["--root", str(repo)])
    assert rc == 0


def test_main_paths_argument_scans_only_listed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src" / "leak.py").write_text("user = 'lutz@gmail.com'\n")
    (repo / "src" / "clean2.py").write_text("x = 1\n")
    # When pre-commit passes --paths src/clean2.py, the leak in leak.py
    # is not scanned.
    rc = main(["--root", str(repo), "--paths", "src/clean2.py"])
    assert rc == 0


def test_main_invalid_root_returns_two(tmp_path: Path) -> None:
    rc = main(["--root", str(tmp_path / "does-not-exist")])
    assert rc == 2


def test_main_list_rules(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--list-rules"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "email-non-placeholder" in out
    assert "personal-string" in out
    assert "secret-scheme-reference" in out
    assert "operator-narrative-leak" in out


# ─── operator-narrative-leak rule ─────────────────────────────────────


from scripts.boundary_check import scan_file  # noqa: E402


class TestNarrativeLeakHeuristics:
    """Public family-relation heuristics fire on operator-narrative
    prose that no public-mechanism doc should contain. These don't
    depend on a private-terms file — they're vendor-neutral by design."""

    def test_the_operators_wife_caught(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("In production the operator's wife runs the cascade nightly.\n")
        v = scan_file(f, root=tmp_path)
        assert any(x.rule == "operator-narrative-leak" for x in v)

    def test_the_operators_husband_caught(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("The operator's husband approved the change.\n")
        v = scan_file(f, root=tmp_path)
        assert any(x.rule == "operator-narrative-leak" for x in v)

    def test_the_operators_partner_caught(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("the operator's partner reviewed it\n")
        v = scan_file(f, root=tmp_path)
        assert any(x.rule == "operator-narrative-leak" for x in v)

    def test_the_operators_children_caught(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("The operator's children attend the local school.\n")
        v = scan_file(f, root=tmp_path)
        assert any(x.rule == "operator-narrative-leak" for x in v)

    def test_possessive_name_with_setup_caught(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("In Alice's setup the agent runs locally.\n")
        v = scan_file(f, root=tmp_path)
        assert any(x.rule == "operator-narrative-leak" for x in v)

    def test_possessive_name_with_inbox_caught(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("Bob's inbox averages 200 messages a day.\n")
        v = scan_file(f, root=tmp_path)
        assert any(x.rule == "operator-narrative-leak" for x in v)

    def test_technical_possessive_not_flagged(self, tmp_path):
        """High-confidence design: 'Slack's API', 'Pydantic's model'
        and similar generic technical possessives are NOT narrative
        leaks. The noun list is specific to personal-narrative shapes."""
        f = tmp_path / "ok.py"
        f.write_text(
            "# Slack's API responses validate via Pydantic's model_validate.\n"
            "# OpenAI's response_format guarantees the schema.\n"
            "# The cascade's runtime is per-tier configurable.\n"
        )
        v = scan_file(f, root=tmp_path)
        narrative = [x for x in v if x.rule == "operator-narrative-leak"]
        assert not narrative, [x.snippet for x in narrative]

    def test_generic_operator_not_flagged(self, tmp_path):
        """'the operator's setup' should NOT fire (setup is too
        generic — only the family-relation noun list triggers
        the public heuristic)."""
        f = tmp_path / "ok.md"
        f.write_text("The operator's setup uses a single Mac.\n")
        v = scan_file(f, root=tmp_path)
        narrative = [x for x in v if x.rule == "operator-narrative-leak"]
        assert not narrative


class TestNarrativeLeakInAllowlistedFile:
    """The whole point of this rule: it fires EVEN in allowlisted
    files. Allowlist exempts standard rules (op://, /Users/...) for
    legitimate documentation; it should NEVER permit personal
    narrative."""

    def test_narrative_leak_fires_in_allowlisted_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ):
        repo = tmp_path
        (repo / "src").mkdir()
        (repo / "src" / "narrative.md").write_text(
            "# Doc\n\nThe operator's wife runs the daily report.\n"
        )
        (repo / ALLOWLIST_FILENAME).write_text(
            "src/narrative.md\n# allowlisted for testing\n"
        )
        rc = main(["--root", str(repo)])
        assert rc == 1, "narrative leak must fire even in allowlisted file"
        err = capsys.readouterr().err
        assert "operator-narrative-leak" in err
