"""LangGraph runtime for deeper contact investigation on ambiguous emails."""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict

from app.connectors.calendar import CalendarHistoryConnector
from app.connectors.gmail_history import GmailHistoryConnector
from app.connectors.linkedin_dataset import LinkedInDatasetConnector
from app.control_plane.graph_runtime import GraphRuntimeManager
from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.contact_drafting import ContactInvestigationDraftWriterTool
from app.tools.contact_investigation import ContactRelationshipAnalyzerTool
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.contact_investigation_models import (
    ContactInvestigationAssessment,
    ContactInvestigationDraftPackage,
    ContactInvestigationItem,
)
from app.workers.email_models import EmailClassification, EmailMessage


class ContactInvestigationRuntimeContext(BaseModel):
    """Serializable workflow context shared across one contact-investigation run."""

    model_config = ConfigDict(extra="forbid")

    operator_email: str
    prompts_by_tool_id: dict[str, PromptDocument]
    tool_definitions: list[WorkflowToolDefinition]


class ContactInvestigationGraphState(TypedDict, total=False):
    run_id: str
    workflow_id: str
    runtime_context: dict[str, Any]
    message: dict[str, Any]
    email_classification: dict[str, Any]
    gmail_history: dict[str, Any]
    calendar_history: dict[str, Any]
    linkedin: dict[str, Any]
    assessment: dict[str, Any]
    draft: dict[str, Any]
    tool_records: list[dict[str, Any]]


class ContactInvestigationStateGraphRuntime:
    """Compile and invoke the per-message contact-investigation graph."""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime_manager: GraphRuntimeManager,
        gmail_history: GmailHistoryConnector,
        calendar_history: CalendarHistoryConnector,
        linkedin_lookup: LinkedInDatasetConnector,
    ) -> None:
        self.settings = settings
        self.runtime_manager = runtime_manager
        self.gmail_history = gmail_history
        self.calendar_history = calendar_history
        self.linkedin_lookup = linkedin_lookup
        self._message_graph = self._build_message_graph()

    def process_candidates(
        self,
        *,
        run_id: str,
        workflow_id: str,
        messages: list[EmailMessage],
        email_classifications: list[EmailClassification],
        runtime_context: ContactInvestigationRuntimeContext,
    ) -> list[ContactInvestigationItem]:
        serialized_context = runtime_context.model_dump(mode="json")
        inputs: list[ContactInvestigationGraphState] = [
            {
                "run_id": run_id,
                "workflow_id": workflow_id,
                "runtime_context": serialized_context,
                "message": message.model_dump(mode="json"),
                "email_classification": classification.model_dump(mode="json"),
                "tool_records": [],
            }
            for message, classification in zip(messages, email_classifications, strict=True)
        ]
        configs = [
            self.runtime_manager.runnable_config(
                run_id=run_id,
                workflow_id=workflow_id,
                thread_suffix=f"contact:{message.message_id}",
            )
            for message in messages
        ]
        raw_results = self._message_graph.batch(inputs, config=configs)
        return [self._result_from_state(result) for result in raw_results]

    def _build_message_graph(self) -> Any:
        graph = StateGraph(ContactInvestigationGraphState)
        graph.add_node("gmail_history_enrichment", self._run_gmail_history)
        graph.add_node("calendar_history_enrichment", self._run_calendar_history)
        graph.add_node("linkedin_enrichment", self._run_linkedin_lookup)
        graph.add_node("relationship_analysis", self._run_relationship_analysis)
        graph.add_node("investigation_draft_writer", self._run_draft_writer)
        graph.add_node("gmail_draft_creator", self._run_gmail_draft_creator)

        graph.add_edge(START, "gmail_history_enrichment")
        graph.add_edge("gmail_history_enrichment", "calendar_history_enrichment")
        graph.add_edge("calendar_history_enrichment", "linkedin_enrichment")
        graph.add_edge("linkedin_enrichment", "relationship_analysis")
        graph.add_edge("relationship_analysis", "investigation_draft_writer")
        graph.add_edge("investigation_draft_writer", "gmail_draft_creator")
        graph.add_edge("gmail_draft_creator", END)
        return graph.compile(checkpointer=self.runtime_manager.checkpointer)

    def _run_gmail_history(self, state: ContactInvestigationGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        message = _message(state)
        tool_definition = _tool_or_raise(
            _tool_map(context.tool_definitions),
            "gmail_history_enrichment",
        )
        summary = self.gmail_history.summarize_contact(
            contact_email=message.from_email,
            calendar_link=self.settings.meeting_calendar_link,
        )
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details=summary,
        )
        return {
            "gmail_history": summary,
            "tool_records": _append_record(state, record),
        }

    def _run_calendar_history(self, state: ContactInvestigationGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        message = _message(state)
        tool_definition = _tool_or_raise(
            _tool_map(context.tool_definitions), "calendar_history_enrichment"
        )
        summary = self.calendar_history.summarize_contact(
            contact_email=message.from_email,
            contact_name=message.from_name,
        )
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details=summary,
        )
        return {
            "calendar_history": summary,
            "tool_records": _append_record(state, record),
        }

    def _run_linkedin_lookup(self, state: ContactInvestigationGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        message = _message(state)
        tool_definition = _tool_or_raise(_tool_map(context.tool_definitions), "linkedin_enrichment")
        summary = self.linkedin_lookup.lookup_contact(
            email=message.from_email,
            display_name=message.from_name,
        )
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details=summary,
        )
        return {
            "linkedin": summary,
            "tool_records": _append_record(state, record),
        }

    def _run_relationship_analysis(self, state: ContactInvestigationGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        tool_map = _tool_map(context.tool_definitions)
        tool_definition = _tool_or_raise(tool_map, "contact_relationship_analyzer")
        prompt = _prompt_or_raise(context.prompts_by_tool_id, tool_definition.tool_id)
        analyzer = ContactRelationshipAnalyzerTool(
            tool_id=tool_definition.tool_id,
            analyzer_config=prompt.config,
        )
        assessment, record = analyzer.assess(
            message=_message(state),
            email_classification=EmailClassification.model_validate(state["email_classification"]),
            gmail_history=dict(state["gmail_history"]),
            calendar_history=dict(state["calendar_history"]),
            linkedin=dict(state["linkedin"]),
        )
        return {
            "assessment": assessment.model_dump(mode="json"),
            "tool_records": _append_record(state, record),
        }

    def _run_draft_writer(self, state: ContactInvestigationGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        tool_definition = _tool_or_raise(
            _tool_map(context.tool_definitions), "contact_investigation_draft_writer"
        )
        prompt = _prompt_or_raise(context.prompts_by_tool_id, tool_definition.tool_id)
        mailbox_summaries = list(dict(state["gmail_history"]).get("mailbox_summaries", []))
        draft_recipient = context.operator_email
        if mailbox_summaries:
            first_mailbox_account = str(mailbox_summaries[0].get("mailbox_account", "")).strip()
            if first_mailbox_account:
                draft_recipient = first_mailbox_account
        writer = ContactInvestigationDraftWriterTool(
            tool_id=tool_definition.tool_id,
            template_config=prompt.config,
            operator_email=context.operator_email,
        )
        draft, record = writer.write(
            message=_message(state),
            email_classification=EmailClassification.model_validate(state["email_classification"]),
            gmail_history=dict(state["gmail_history"]),
            calendar_history=dict(state["calendar_history"]),
            linkedin=dict(state["linkedin"]),
            assessment=ContactInvestigationAssessment.model_validate(state["assessment"]),
            draft_recipient=draft_recipient,
        )
        return {
            "draft": draft.model_dump(mode="json"),
            "tool_records": _append_record(state, record),
        }

    def _run_gmail_draft_creator(self, state: ContactInvestigationGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        tool_definition = _tool_or_raise(_tool_map(context.tool_definitions), "gmail_draft_creator")
        draft = ContactInvestigationDraftPackage.model_validate(state["draft"])
        created = self.gmail_history.create_draft(
            to_email=draft.to_email,
            subject=draft.draft_subject,
            body=draft.draft_body,
            thread_id=draft.thread_id,
        )
        updated = draft.model_copy(update={"draft_created": True, "draft_id": created["draft_id"]})
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details=created,
        )
        return {
            "draft": updated.model_dump(mode="json"),
            "tool_records": _append_record(state, record),
        }

    def _result_from_state(self, state: ContactInvestigationGraphState) -> ContactInvestigationItem:
        return ContactInvestigationItem(
            message=EmailMessage.model_validate(state["message"]),
            email_classification=EmailClassification.model_validate(state["email_classification"]),
            gmail_history=dict(state["gmail_history"]),
            calendar_history=dict(state["calendar_history"]),
            linkedin=dict(state["linkedin"]),
            assessment=ContactInvestigationAssessment.model_validate(state["assessment"]),
            draft=ContactInvestigationDraftPackage.model_validate(state["draft"]),
            tool_records=[
                ToolExecutionRecord.model_validate(record)
                for record in state.get("tool_records", [])
            ],
        )


def _runtime_context(state: ContactInvestigationGraphState) -> ContactInvestigationRuntimeContext:
    return ContactInvestigationRuntimeContext.model_validate(state["runtime_context"])


def _message(state: ContactInvestigationGraphState) -> EmailMessage:
    return EmailMessage.model_validate(state["message"])


def _tool_map(
    tool_definitions: list[WorkflowToolDefinition],
) -> dict[str, WorkflowToolDefinition]:
    return {tool.kind: tool for tool in tool_definitions if tool.enabled}


def _tool_or_raise(
    tool_map: dict[str, WorkflowToolDefinition], kind: str
) -> WorkflowToolDefinition:
    if kind not in tool_map:
        raise KeyError(f"Workflow is missing required tool kind: {kind}")
    return tool_map[kind]


def _prompt_or_raise(
    prompts_by_tool_id: dict[str, PromptDocument], tool_id: str
) -> PromptDocument:
    if tool_id not in prompts_by_tool_id:
        raise KeyError(f"Workflow is missing prompt for tool id: {tool_id}")
    return prompts_by_tool_id[tool_id]


def _append_record(
    state: ContactInvestigationGraphState, record: ToolExecutionRecord
) -> list[dict[str, Any]]:
    current = list(state.get("tool_records", []))
    current.append(record.model_dump(mode="json"))
    return current
