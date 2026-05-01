"""Tests for the public reply-parser factories.

These factories are TASK-AGNOSTIC — the option list is provided by the
caller (private overlay typically). Tests use a generic 3-bucket
taxonomy that exercises the contract without depending on any specific
task's vocabulary.
"""

from __future__ import annotations

import pytest

from app.eval.reply_parsers import (
    DEFAULT_SKIP_KEYWORDS,
    option_matching_parser,
    permissive_text_parser,
)


@pytest.fixture
def generic_l1_parser():
    return option_matching_parser(
        options=["customers", "personal", "newsletters"],
        output_field="level1_classification",
    )


@pytest.fixture
def two_level_parser():
    return option_matching_parser(
        options=["customers", "personal", "newsletters"],
        output_field="level1_classification",
        second_field="level2_intent",
        second_field_options=["informational", "action_required"],
    )


def test_exact_match_returns_canonical_value(generic_l1_parser) -> None:
    result = generic_l1_parser("customers")
    assert result == {"level1_classification": "customers", "valid": True}


def test_match_is_case_insensitive_by_default(generic_l1_parser) -> None:
    assert generic_l1_parser("CUSTOMERS")["level1_classification"] == "customers"
    assert generic_l1_parser("Customers")["level1_classification"] == "customers"


def test_unknown_option_is_invalid_and_reports_expected(generic_l1_parser) -> None:
    result = generic_l1_parser("no idea")
    assert result["valid"] is False
    assert result["expected_options"] == ["customers", "personal", "newsletters"]
    assert "not a recognized" in result["reason"]
    assert result["text"] == "no idea"


def test_empty_reply_is_invalid(generic_l1_parser) -> None:
    result = generic_l1_parser("")
    assert result["valid"] is False
    assert "empty" in result["reason"]


def test_whitespace_only_reply_is_invalid(generic_l1_parser) -> None:
    result = generic_l1_parser("   \t  ")
    assert result["valid"] is False


def test_skip_keywords_mark_record_as_skipped(generic_l1_parser) -> None:
    for keyword in DEFAULT_SKIP_KEYWORDS:
        result = generic_l1_parser(keyword)
        assert result["valid"] is True, f"`{keyword}` should be valid (with skipped=True)"
        assert result["skipped"] is True


def test_two_field_parser_extracts_both_tokens(two_level_parser) -> None:
    result = two_level_parser("customers action_required")
    assert result["level1_classification"] == "customers"
    assert result["level2_intent"] == "action_required"
    assert result["valid"] is True


def test_two_field_parser_with_unknown_second_token_keeps_first(
    two_level_parser,
) -> None:
    result = two_level_parser("customers urgent")
    assert result["level1_classification"] == "customers"
    assert result["valid"] is True
    assert "level2_intent" not in result
    assert "note" in result
    assert "urgent" in result["note"]


def test_comma_separator_works(two_level_parser) -> None:
    result = two_level_parser("customers, action_required")
    assert result["level1_classification"] == "customers"
    assert result["level2_intent"] == "action_required"


def test_case_sensitive_mode_distinguishes() -> None:
    parser = option_matching_parser(
        options=["Customers", "Personal"],
        output_field="bucket",
        case_sensitive=True,
    )
    assert parser("Customers")["bucket"] == "Customers"
    assert parser("customers")["valid"] is False  # case mismatch


def test_options_required_to_be_non_empty() -> None:
    with pytest.raises(ValueError):
        option_matching_parser(options=[], output_field="x")


def test_second_field_options_required_when_second_field_set() -> None:
    with pytest.raises(ValueError):
        option_matching_parser(
            options=["a"],
            output_field="x",
            second_field="y",
            # missing second_field_options
        )


def test_permissive_parser_always_valid() -> None:
    result = permissive_text_parser("anything goes here")
    assert result["valid"] is True
    assert result["text"] == "anything goes here"


def test_skip_keywords_can_be_overridden() -> None:
    parser = option_matching_parser(
        options=["a", "b"],
        output_field="x",
        skip_keywords=("nope",),
    )
    # default "skip" keyword is no longer recognized as skip
    assert parser("skip")["valid"] is False
    assert parser("nope")["valid"] is True
    assert parser("nope")["skipped"] is True


def test_first_token_only_matters_when_no_second_field(generic_l1_parser) -> None:
    """For a single-field parser, trailing tokens are tolerated and ignored."""

    result = generic_l1_parser("customers please")
    assert result["valid"] is True
    assert result["level1_classification"] == "customers"
