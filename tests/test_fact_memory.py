from __future__ import annotations

from app.learning.fact_memory import FactMemoryStore, extract_operator_facts
from app.shared.config import Settings


def test_fact_memory_extracts_and_retrieves_operator_facts(test_settings: Settings) -> None:
    store = FactMemoryStore(test_settings.database_path)
    records = extract_operator_facts(
        text=(
            "My home is 1450 Wildrose Way, 94043. "
            "I am teaching in Sage Hall in Ithaca. "
            "Please help with this flight."
        ),
        source_workflow_id="sai-email-interaction",
        source_run_id="run-facts-1",
        source_reference="Travel planning help",
        source_thread_id="thread-facts-1",
        source_message_id="msg-facts-1",
    )

    assert store.record_facts(records) == 2

    relevant = store.query_relevant_facts(
        query_text="Please help me plan the airport travel and calendar blocks.",
        workflow_id="sai-email-interaction",
        limit=5,
    )
    assert {record.fact_key for record in relevant} == {"home_address", "teaching_location"}
    assert any("Wildrose" in record.value for record in relevant)
    assert any("Sage Hall" in record.value for record in relevant)


def test_fact_memory_versions_conflicts_and_restricts_sensitive_access(
    test_settings: Settings,
) -> None:
    store = FactMemoryStore(test_settings.database_path)
    first = extract_operator_facts(
        text="My home is 1450 Wildrose Way, 94043.",
        source_workflow_id="sai-email-interaction",
        source_run_id="run-facts-1",
        source_reference="Initial fact",
        source_thread_id="thread-facts-1",
        source_message_id="msg-facts-1",
    )
    second = extract_operator_facts(
        text="My home is 1 Infinite Loop, Cupertino, CA.",
        source_workflow_id="sai-email-interaction",
        source_run_id="run-facts-2",
        source_reference="Updated fact",
        source_thread_id="thread-facts-2",
        source_message_id="msg-facts-2",
    )

    assert store.record_facts(first) == 1
    assert store.record_facts(second) == 1

    relevant = store.query_relevant_facts(
        query_text="Plan airport travel from home.",
        workflow_id="sai-email-interaction",
        limit=5,
    )
    assert len(relevant) == 1
    assert relevant[0].value == "1 Infinite Loop, Cupertino, CA"
    assert relevant[0].version == 2
    assert relevant[0].sensitive is True

    history = store.list_fact_history(fact_key="home_address")
    assert [record.version for record in history] == [1, 2]
    assert history[0].status == "superseded"
    assert history[1].status == "active"
    assert history[1].supersedes_fact_id == history[0].fact_id

    other_workflow = store.query_relevant_facts(
        query_text="What is home?",
        workflow_id="newsletter-summary-daily",
        limit=5,
    )
    assert other_workflow == []
