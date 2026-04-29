"""LangGraph runtime for the Phase 1 meeting-decision workflow."""

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
from app.tools.meeting_drafting import InternalBriefWriterTool, ReplyDraftWriterTool
from app.tools.meeting_likelihood import MeetingLikelihoodPredictorTool
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.tools.request_type_classifier import RequestTypeClassifierTool
from app.workers.email_models import EmailClassification, EmailMessage
from app.workers.meeting_models import (
    MeetingDecisionItem,
    MeetingDraftPackage,
    MeetingEvidence,
    MeetingLikelihoodAssessment,
)


class MeetingDecisionRuntimeContext(BaseModel):
    """Serializable workflow context shared across one meeting-decision run."""

    model_config = ConfigDict(extra="forbid")

    operator_email: str
    calendar_link: str
    prompts_by_tool_id: dict[str, PromptDocument]
    tool_definitions: list[WorkflowToolDefinition]


class MeetingDecisionGraphState(TypedDict, total=False):
    run_id: str
    workflow_id: str
    runtime_context: dict[str, Any]
    message: dict[str, Any]
    email_classification: dict[str, Any]
    evidence: dict[str, Any]
    assessment: dict[str, Any]
    internal_note: str
    draft: dict[str, Any]
    tool_records: list[dict[str, Any]]


class MeetingDecisionStateGraphRuntime:
    """Compile and invoke the per-message meeting decision graph."""

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
        runtime_context: MeetingDecisionRuntimeContext,
    ) -> list[MeetingDecisionItem]:
        serialized_context = runtime_context.model_dump(mode="json")
        inputs: list[MeetingDecisionGraphState] = [
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
                thread_suffix=f"meeting:{message.message_id}",
            )
            for message in messages
        ]
        raw_results = self._message_graph.batch(inputs, config=configs)
        return [self._result_from_state(result) for result in raw_results]

    def _build_message_graph(self) -> Any:
        graph = StateGraph(MeetingDecisionGraphState)
        graph.add_node("gmail_history_enrichment", self._run_gmail_history)
        graph.add_node("calendar_history_enrichment", self._run_calendar_history)
        graph.add_node("linkedin_enrichment", self._run_linkedin_lookup)
        graph.add_node("request_type_classifier", self._run_request_type)
        graph.add_node("likelihood_predictor", self._run_likelihood)
        graph.add_node("internal_brief_writer", self._run_internal_brief)
        graph.add_node("reply_draft_writer", self._run_reply_draft)
        graph.add_node("gmail_draft_creator", self._run_gmail_draft_creator)
        graph.add_node("draft_skipped", self._mark_draft_skipped)

        graph.add_edge(START, "gmail_history_enrichment")
        graph.add_edge("gmail_history_enrichment", "calendar_history_enrichment")
        graph.add_edge("calendar_history_enrichment", "linkedin_enrichment")
        graph.add_edge("linkedin_enrichment", "request_type_classifier")
        graph.add_edge("request_type_classifier", "likelihood_predictor")
        graph.add_edge("likelihood_predictor", "internal_brief_writer")
        graph.add_edge("internal_brief_writer", "reply_draft_writer")
        graph.add_conditional_edges(
            "reply_draft_writer",
            self._route_after_reply_draft,
            {
                "gmail_draft_creator": "gmail_draft_creator",
                "draft_skipped": "draft_skipped",
            },
        )
        graph.add_edge("gmail_draft_creator", END)
        graph.add_edge("draft_skipped", END)
        return graph.compile(checkpointer=self.runtime_manager.checkpointer)

    def _run_gmail_history(self, state: MeetingDecisionGraphState) -> dict[str, Any]:
        message = _message(state)
        context = _runtime_context(state)
        tool_definition = _tool_or_raise(
            _tool_map(context.tool_definitions),
            "gmail_history_enrichment",
        )
        summary = self.gmail_history.summarize_contact(
            contact_email=message.from_email,
            calendar_link=context.calendar_link,
        )
        evidence = MeetingEvidence(
            message_id=message.message_id,
            contact_email=message.from_email,
            contact_name=message.from_name,
            gmail_history=summary,
            calendar_history={},
            meetings_in_last_12_months=0,
            met_before_in_last_12_months=False,
            last_meeting_at=None,
            linkedin={},
            request_type="unknown",
        )
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details=summary,
        )
        return {
            "evidence": evidence.model_dump(mode="json"),
            "tool_records": _append_record(state, record),
        }

    def _run_calendar_history(self, state: MeetingDecisionGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        evidence = MeetingEvidence.model_validate(state["evidence"])
        tool_definition = _tool_or_raise(
            _tool_map(context.tool_definitions),
            "calendar_history_enrichment",
        )
        summary = self.calendar_history.summarize_contact(
            contact_email=evidence.contact_email,
            contact_name=evidence.contact_name,
        )
        updated = evidence.model_copy(
            update={
                "calendar_history": summary,
                "meetings_in_last_12_months": int(
                    summary.get(
                        "meetings_in_last_12_months",
                        summary.get("prior_meeting_count", 0),
                    )
                ),
                "met_before_in_last_12_months": bool(
                    summary.get(
                        "met_before_in_last_12_months",
                        summary.get("has_prior_meeting", False),
                    )
                ),
                "last_meeting_at": summary.get("last_meeting_at"),
            }
        )
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details=summary,
        )
        return {
            "evidence": updated.model_dump(mode="json"),
            "tool_records": _append_record(state, record),
        }

    def _run_linkedin_lookup(self, state: MeetingDecisionGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        message = _message(state)
        evidence = MeetingEvidence.model_validate(state["evidence"])
        tool_definition = _tool_or_raise(_tool_map(context.tool_definitions), "linkedin_enrichment")
        summary = self.linkedin_lookup.lookup_contact(
            email=evidence.contact_email,
            display_name=message.from_name,
        )
        updated = evidence.model_copy(update={"linkedin": summary})
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details=summary,
        )
        return {
            "evidence": updated.model_dump(mode="json"),
            "tool_records": _append_record(state, record),
        }

    def _run_request_type(self, state: MeetingDecisionGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        message = _message(state)
        tool_definition = _tool_or_raise(
            _tool_map(context.tool_definitions),
            "request_type_classifier",
        )
        prompt = _prompt_or_raise(context.prompts_by_tool_id, tool_definition.tool_id)
        classifier = RequestTypeClassifierTool(
            tool_id=tool_definition.tool_id,
            classifier_config=prompt.config,
        )
        request_type, record = classifier.classify(message=message)
        evidence = MeetingEvidence.model_validate(state["evidence"]).model_copy(
            update={"request_type": request_type}
        )
        return {
            "evidence": evidence.model_dump(mode="json"),
            "tool_records": _append_record(state, record),
        }

    def _run_likelihood(self, state: MeetingDecisionGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        tool_definition = _tool_or_raise(
            _tool_map(context.tool_definitions),
            "meeting_likelihood_predictor",
        )
        prompt = _prompt_or_raise(context.prompts_by_tool_id, tool_definition.tool_id)
        predictor = MeetingLikelihoodPredictorTool(
            tool_id=tool_definition.tool_id,
            predictor_config=prompt.config,
        )
        evidence = MeetingEvidence.model_validate(state["evidence"])
        assessment, record = predictor.assess(evidence=evidence)
        return {
            "assessment": assessment.model_dump(mode="json"),
            "tool_records": _append_record(state, record),
        }

    def _run_internal_brief(self, state: MeetingDecisionGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        tool_definition = _tool_or_raise(
            _tool_map(context.tool_definitions),
            "internal_brief_writer",
        )
        prompt = _prompt_or_raise(context.prompts_by_tool_id, tool_definition.tool_id)
        writer = InternalBriefWriterTool(
            tool_id=tool_definition.tool_id,
            template_config=prompt.config,
        )
        note, record = writer.write(
            message=_message(state),
            evidence=MeetingEvidence.model_validate(state["evidence"]),
            assessment=MeetingLikelihoodAssessment.model_validate(state["assessment"]),
        )
        return {
            "internal_note": note,
            "tool_records": _append_record(state, record),
        }

    def _run_reply_draft(self, state: MeetingDecisionGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        tool_definition = _tool_or_raise(_tool_map(context.tool_definitions), "reply_draft_writer")
        prompt = _prompt_or_raise(context.prompts_by_tool_id, tool_definition.tool_id)
        writer = ReplyDraftWriterTool(
            tool_id=tool_definition.tool_id,
            template_config=prompt.config,
            calendar_link=context.calendar_link,
        )
        draft, record = writer.write(
            message=_message(state),
            assessment=MeetingLikelihoodAssessment.model_validate(state["assessment"]),
            internal_note=str(state["internal_note"]),
        )
        return {
            "draft": draft.model_dump(mode="json"),
            "tool_records": _append_record(state, record),
        }

    def _route_after_reply_draft(self, state: MeetingDecisionGraphState) -> str:
        assessment = MeetingLikelihoodAssessment.model_validate(state["assessment"])
        return "gmail_draft_creator" if assessment.should_create_draft else "draft_skipped"

    def _run_gmail_draft_creator(self, state: MeetingDecisionGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        message = _message(state)
        draft = MeetingDraftPackage.model_validate(state["draft"])
        tool_definition = _tool_or_raise(_tool_map(context.tool_definitions), "gmail_draft_creator")
        created = self.gmail_history.create_draft(
            to_email=message.from_email,
            subject=draft.external_subject,
            body=draft.external_body,
            thread_id=message.thread_id,
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

    def _mark_draft_skipped(self, state: MeetingDecisionGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        tool_definition = _tool_or_raise(_tool_map(context.tool_definitions), "gmail_draft_creator")
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.SKIPPED,
            details={"reason": "decision_did_not_require_draft"},
        )
        return {"tool_records": _append_record(state, record)}

    def _result_from_state(self, state: MeetingDecisionGraphState) -> MeetingDecisionItem:
        return MeetingDecisionItem(
            message=EmailMessage.model_validate(state["message"]),
            email_classification=EmailClassification.model_validate(state["email_classification"]),
            evidence=MeetingEvidence.model_validate(state["evidence"]),
            assessment=MeetingLikelihoodAssessment.model_validate(state["assessment"]),
            draft=MeetingDraftPackage.model_validate(state["draft"]),
            tool_records=[
                ToolExecutionRecord.model_validate(record)
                for record in state.get("tool_records", [])
            ],
        )


def _runtime_context(state: MeetingDecisionGraphState) -> MeetingDecisionRuntimeContext:
    return MeetingDecisionRuntimeContext.model_validate(state["runtime_context"])


def _message(state: MeetingDecisionGraphState) -> EmailMessage:
    return EmailMessage.model_validate(state["message"])


def _append_record(
    state: MeetingDecisionGraphState,
    record: ToolExecutionRecord,
) -> list[dict[str, Any]]:
    records = list(state.get("tool_records", []))
    records.append(record.model_dump(mode="json"))
    return records


def _tool_map(tool_definitions: list[WorkflowToolDefinition]) -> dict[str, WorkflowToolDefinition]:
    return {tool.kind: tool for tool in tool_definitions if tool.enabled}


def _tool_or_raise(
    tool_map: dict[str, WorkflowToolDefinition],
    kind: str,
) -> WorkflowToolDefinition:
    tool = tool_map.get(kind)
    if tool is None:
        raise KeyError(f"Workflow is missing required tool kind: {kind}")
    return tool


def _prompt_or_raise(
    prompts_by_tool_id: dict[str, PromptDocument],
    tool_id: str,
) -> PromptDocument:
    prompt = prompts_by_tool_id.get(tool_id)
    if prompt is None:
        raise KeyError(f"Workflow is missing required prompt for tool_id: {tool_id}")
    return prompt
