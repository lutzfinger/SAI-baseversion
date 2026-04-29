"""LangGraph runtime for reply planning and approval-draft creation."""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict

from app.connectors.gmail_drafts import GmailDraftConnector
from app.control_plane.graph_runtime import GraphRuntimeManager
from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.tools.reply_planning import ReplyPlanDraftWriterTool, ReplyPlanWriterTool
from app.workers.email_models import EmailClassification, EmailMessage
from app.workers.reply_planning_models import (
    ReplyPlan,
    ReplyPlanningDraftPackage,
    ReplyPlanningItem,
)


class ReplyPlanningRuntimeContext(BaseModel):
    """Serializable workflow context shared across one reply-planning run."""

    model_config = ConfigDict(extra="forbid")

    operator_email: str
    prompts_by_tool_id: dict[str, PromptDocument]
    tool_definitions: list[WorkflowToolDefinition]


class ReplyPlanningGraphState(TypedDict, total=False):
    run_id: str
    workflow_id: str
    runtime_context: dict[str, Any]
    message: dict[str, Any]
    email_classification: dict[str, Any]
    plan: dict[str, Any]
    draft: dict[str, Any]
    tool_records: list[dict[str, Any]]


class ReplyPlanningStateGraphRuntime:
    """Compile and invoke the per-message reply-planning graph."""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime_manager: GraphRuntimeManager,
        gmail_drafts: GmailDraftConnector,
    ) -> None:
        self.settings = settings
        self.runtime_manager = runtime_manager
        self.gmail_drafts = gmail_drafts
        self._message_graph = self._build_message_graph()

    def process_messages(
        self,
        *,
        run_id: str,
        workflow_id: str,
        messages: list[EmailMessage],
        email_classifications: list[EmailClassification],
        runtime_context: ReplyPlanningRuntimeContext,
    ) -> list[ReplyPlanningItem]:
        serialized_context = runtime_context.model_dump(mode="json")
        results: list[ReplyPlanningItem] = []
        for message, classification in zip(messages, email_classifications, strict=True):
            input_state: ReplyPlanningGraphState = {
                "run_id": run_id,
                "workflow_id": workflow_id,
                "runtime_context": serialized_context,
                "message": message.model_dump(mode="json"),
                "email_classification": classification.model_dump(mode="json"),
                "tool_records": [],
            }
            config = self.runtime_manager.runnable_config(
                run_id=run_id,
                workflow_id=workflow_id,
                thread_suffix=f"reply:{message.message_id}",
            )
            raw_result = self._message_graph.invoke(input_state, config=config)
            results.append(self._result_from_state(raw_result))
        return results

    def _build_message_graph(self) -> Any:
        graph = StateGraph(ReplyPlanningGraphState)
        graph.add_node("reply_plan_writer", self._run_reply_plan_writer)
        graph.add_node("reply_plan_draft_writer", self._run_reply_plan_draft_writer)
        graph.add_node("reply_plan_gmail_draft_creator", self._run_gmail_draft_creator)

        graph.add_edge(START, "reply_plan_writer")
        graph.add_edge("reply_plan_writer", "reply_plan_draft_writer")
        graph.add_edge("reply_plan_draft_writer", "reply_plan_gmail_draft_creator")
        graph.add_edge("reply_plan_gmail_draft_creator", END)
        return graph.compile(checkpointer=self.runtime_manager.checkpointer)

    def _run_reply_plan_writer(self, state: ReplyPlanningGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        tool_definition = _tool_or_raise(_tool_map(context.tool_definitions), "reply_plan_writer")
        prompt = _prompt_or_raise(context.prompts_by_tool_id, tool_definition.tool_id)
        writer = ReplyPlanWriterTool(
            tool_definition=tool_definition,
            prompt=prompt,
            settings=self.settings,
        )
        plan, record = writer.plan(
            message=_message(state),
            email_classification=EmailClassification.model_validate(state["email_classification"]),
        )
        return {
            "plan": plan.model_dump(mode="json"),
            "tool_records": _append_record(state, record),
        }

    def _run_reply_plan_draft_writer(self, state: ReplyPlanningGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        tool_definition = _tool_or_raise(
            _tool_map(context.tool_definitions),
            "reply_plan_draft_writer",
        )
        prompt = _prompt_or_raise(context.prompts_by_tool_id, tool_definition.tool_id)
        writer = ReplyPlanDraftWriterTool(
            tool_id=tool_definition.tool_id,
            template_config=prompt.config,
            operator_email=context.operator_email,
        )
        account_email_getter = getattr(self.gmail_drafts, "account_email", None)
        draft_recipient = (
            account_email_getter() if callable(account_email_getter) else ""
        ) or context.operator_email
        draft, record = writer.write(
            message=_message(state),
            email_classification=EmailClassification.model_validate(state["email_classification"]),
            plan=ReplyPlan.model_validate(state["plan"]),
            draft_recipient=draft_recipient,
        )
        return {
            "draft": draft.model_dump(mode="json"),
            "tool_records": _append_record(state, record),
        }

    def _run_gmail_draft_creator(self, state: ReplyPlanningGraphState) -> dict[str, Any]:
        context = _runtime_context(state)
        draft = ReplyPlanningDraftPackage.model_validate(state["draft"])
        tool_definition = _tool_or_raise(_tool_map(context.tool_definitions), "gmail_draft_creator")
        created = self.gmail_drafts.create_draft(
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

    def _result_from_state(self, state: ReplyPlanningGraphState) -> ReplyPlanningItem:
        return ReplyPlanningItem(
            message=EmailMessage.model_validate(state["message"]),
            email_classification=EmailClassification.model_validate(state["email_classification"]),
            plan=ReplyPlan.model_validate(state["plan"]),
            draft=ReplyPlanningDraftPackage.model_validate(state["draft"]),
            tool_records=[
                ToolExecutionRecord.model_validate(record)
                for record in state.get("tool_records", [])
            ],
        )


def _runtime_context(state: ReplyPlanningGraphState) -> ReplyPlanningRuntimeContext:
    return ReplyPlanningRuntimeContext.model_validate(state["runtime_context"])


def _message(state: ReplyPlanningGraphState) -> EmailMessage:
    return EmailMessage.model_validate(state["message"])


def _append_record(
    state: ReplyPlanningGraphState,
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
        raise KeyError(f"Prompt not loaded for tool_id={tool_id}")
    return prompt
