"""Tests for app.llm.cost — CostTable loading + cost computation."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.llm.cost import DEFAULT_COST_TABLE_PATH, CostTable, get_default_cost_table
from app.llm.provider import TokenUsage


def test_default_table_loads() -> None:
    table = get_default_cost_table()
    # gpt-4o is one of the canonical entries; price snapshot may shift but
    # presence and shape should be stable.
    assert table.cost_for(
        provider_id="openai",
        model="gpt-4o",
        usage=TokenUsage(input_tokens=1_000_000, output_tokens=0),
    ) > 0


def test_default_table_path_exists() -> None:
    assert DEFAULT_COST_TABLE_PATH.exists()


def test_unknown_provider_returns_zero() -> None:
    table = get_default_cost_table()
    assert table.cost_for(
        provider_id="unknown_vendor",
        model="x",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    ) == 0.0


def test_unknown_model_falls_back_to_wildcard() -> None:
    table = get_default_cost_table()
    # OpenAI table has a "*" fallback entry — unknown models use it.
    cost = table.cost_for(
        provider_id="openai",
        model="some-future-model-name",
        usage=TokenUsage(input_tokens=1_000_000, output_tokens=0),
    )
    assert cost > 0


def test_cost_calculation_with_synthetic_table(tmp_path: Path) -> None:
    table_path = tmp_path / "synthetic.yaml"
    table_path.write_text(
        "providers:\n"
        "  test_vendor:\n"
        "    test_model:\n"
        "      input: 1.0\n"
        "      cached_input: 0.5\n"
        "      output: 4.0\n",
        encoding="utf-8",
    )
    table = CostTable.from_yaml(table_path)
    cost = table.cost_for(
        provider_id="test_vendor",
        model="test_model",
        usage=TokenUsage(
            input_tokens=2_000_000,
            cached_input_tokens=500_000,
            output_tokens=1_000_000,
        ),
    )
    # 1.5M non-cached @ $1 = $1.50
    # 0.5M cached    @ $0.50 = $0.25
    # 1.0M output    @ $4 = $4.00
    # total = $5.75
    assert cost == pytest.approx(5.75, rel=1e-9)


def test_ollama_costs_zero_by_default() -> None:
    table = get_default_cost_table()
    cost = table.cost_for(
        provider_id="ollama",
        model="gpt-oss:20b",
        usage=TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    assert cost == 0.0
