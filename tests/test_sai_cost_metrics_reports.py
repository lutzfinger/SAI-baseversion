"""Smoke tests for sai_cost_report + sai_metrics_report CLIs.

These are scaffolding for the future #sai-cost / #sai-metrics Slack
agents (per #16i + design_cost_dashboard_slack.md). The agents read
the same data; tests cover the aggregation shape so the agents
can build on a verified foundation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import sai_cost_report, sai_metrics_report


def test_cost_report_collect_with_no_data(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sai_cost_report, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(sai_cost_report, "SAI_EVAL_AGENT_LOG", tmp_path / "agent.jsonl")

    report = sai_cost_report.collect(hours=24)
    assert report.rows == []
    assert report.total_cost_usd == 0.0
    assert "No cost data" in report.note


def test_cost_report_aggregates_agent_log(monkeypatch, tmp_path) -> None:
    log = tmp_path / "agent.jsonl"
    log.write_text("\n".join([
        json.dumps({
            "started_at": "2026-05-03T12:00:00+00:00",
            "cost_usd": 0.005,
        }),
        json.dumps({
            "started_at": "2026-05-03T12:30:00+00:00",
            "cost_usd": 0.012,
        }),
    ]) + "\n")
    monkeypatch.setattr(sai_cost_report, "SAI_EVAL_AGENT_LOG", log)
    monkeypatch.setattr(sai_cost_report, "AUDIT_PATH", tmp_path / "audit.jsonl")

    # 100-year window so the cutoff doesn't filter our test data
    report = sai_cost_report.collect(hours=24 * 365 * 100)
    assert len(report.rows) == 1
    assert report.rows[0].workflow == "sai-eval-agent"
    assert report.rows[0].invocations == 2
    assert abs(report.rows[0].cost_usd - 0.017) < 1e-9


def test_cost_report_text_format_renders_total(monkeypatch, tmp_path) -> None:
    log = tmp_path / "agent.jsonl"
    log.write_text(json.dumps({
        "started_at": "2026-05-03T12:00:00+00:00",
        "cost_usd": 0.10,
    }) + "\n")
    monkeypatch.setattr(sai_cost_report, "SAI_EVAL_AGENT_LOG", log)
    monkeypatch.setattr(sai_cost_report, "AUDIT_PATH", tmp_path / "audit.jsonl")

    report = sai_cost_report.collect(hours=24 * 365 * 100)
    text = sai_cost_report.format_text(report)
    assert "TOTAL" in text
    assert "0.1000" in text


def test_metrics_report_collect_with_no_data(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sai_metrics_report, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(sai_metrics_report, "PROPOSED_DIR", tmp_path / "eval" / "proposed")

    report = sai_metrics_report.collect()
    assert report.datasets == []
    assert "No eval datasets" in report.note


def test_metrics_report_counts_jsonl_lines(monkeypatch, tmp_path) -> None:
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "canaries.jsonl").write_text(
        "\n".join([json.dumps({"id": i}) for i in range(5)]) + "\n",
    )
    (eval_dir / "edge_cases.jsonl").write_text(
        "\n".join([json.dumps({"id": i}) for i in range(12)]) + "\n",
    )
    monkeypatch.setattr(sai_metrics_report, "EVAL_DIR", eval_dir)
    monkeypatch.setattr(sai_metrics_report, "PROPOSED_DIR", eval_dir / "proposed")

    report = sai_metrics_report.collect()
    by_name = {d.name: d for d in report.datasets}
    assert by_name["canaries"].count == 5
    assert by_name["edge_cases"].count == 12


def test_metrics_report_includes_true_north_when_asked(
    monkeypatch, tmp_path,
) -> None:
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "email-triage_true_north.jsonl").write_text(
        "\n".join([json.dumps({"id": i}) for i in range(3)]) + "\n",
    )
    monkeypatch.setattr(sai_metrics_report, "EVAL_DIR", eval_dir)
    monkeypatch.setattr(sai_metrics_report, "PROPOSED_DIR", eval_dir / "proposed")

    report = sai_metrics_report.collect(include_true_north=True)
    names = {d.name for d in report.datasets}
    assert "true_north:email-triage" in names


def test_metrics_report_proposals_count(monkeypatch, tmp_path) -> None:
    eval_dir = tmp_path / "eval"
    proposed = eval_dir / "proposed"
    proposed.mkdir(parents=True)
    (proposed / "proposed_rule_add_1.yaml").write_text("foo: bar\n")
    (proposed / "proposed_eval_add_2.yaml").write_text("baz: qux\n")
    monkeypatch.setattr(sai_metrics_report, "EVAL_DIR", eval_dir)
    monkeypatch.setattr(sai_metrics_report, "PROPOSED_DIR", proposed)

    report = sai_metrics_report.collect()
    assert report.proposals is not None
    assert report.proposals.open_count == 2
    assert len(report.proposals.paths) == 2
