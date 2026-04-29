"""Structured reply-planning tools for approval-first email workflows."""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any

from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.local_llm_classifier import OpenAILLMClient, llm_exception_details
from app.tools.models import (
    ToolExecutionError,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from app.workers.email_models import (
    LEVEL1_DISPLAY_NAMES,
    LEVEL2_DISPLAY_NAMES,
    EmailClassification,
    EmailMessage,
)
from app.workers.reply_planning_models import ReplyPlan, ReplyPlanningDraftPackage


class ReplyPlanWriterTool:
    """Use a structured cloud LLM call to draft a reply-planning record."""

    def __init__(
        self,
        *,
        tool_definition: WorkflowToolDefinition,
        prompt: PromptDocument,
        settings: Settings,
    ) -> None:
        self.tool_definition = tool_definition
        self.prompt = prompt
        self.settings = settings
        self.provider = tool_definition.provider or "openai"
        self.model = tool_definition.model or "gpt-5.2-pro"
        self.timeout_seconds = _resolve_timeout_seconds(tool_definition, settings)
        self.max_output_tokens = _resolve_max_output_tokens(tool_definition)
        if self.provider != "openai":
            raise ValueError(
                "ReplyPlanWriterTool currently supports only the OpenAI provider"
            )
        self.client: OpenAILLMClient | None = None

    def plan(
        self,
        *,
        message: EmailMessage,
        email_classification: EmailClassification,
    ) -> tuple[ReplyPlan, ToolExecutionRecord]:
        prompt_text = self._render_prompt(
            message=message,
            email_classification=email_classification,
        )
        if self.client is None:
            self.client = OpenAILLMClient(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
                timeout_seconds=self.timeout_seconds,
                settings=self.settings,
                tracing_name=self.tool_definition.tool_id,
                tracing_metadata={
                    "tool_id": self.tool_definition.tool_id,
                    "tool_kind": self.tool_definition.kind,
                    "provider": self.provider,
                },
            )
        started = perf_counter()
        try:
            response = self.client.classify(
                prompt=prompt_text,
                model=self.model,
                response_schema=ReplyPlan.model_json_schema(),
                response_model=ReplyPlan,
                max_output_tokens=self.max_output_tokens,
            )
            elapsed_ms = int((perf_counter() - started) * 1000)
            plan = ReplyPlan.model_validate(response.payload)
        except Exception as exc:
            elapsed_ms = int((perf_counter() - started) * 1000)
            details = {
                "provider": self.provider,
                "model": self.model,
                "timeout_seconds": self.timeout_seconds,
                "max_output_tokens": self.max_output_tokens,
                "elapsed_ms": elapsed_ms,
                **llm_exception_details(exc),
            }
            record = ToolExecutionRecord(
                tool_id=self.tool_definition.tool_id,
                tool_kind=self.tool_definition.kind,
                status=ToolExecutionStatus.FAILED,
                details=details,
            )
            raise ToolExecutionError(
                f"Reply plan writer failed: {exc}",
                tool_record=record,
                context={
                    "message_id": message.message_id,
                    "thread_id": message.thread_id,
                    "from_email": message.from_email,
                    "subject": message.subject,
                },
            ) from exc

        record = ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "provider": self.provider,
                "model": self.model,
                "raw_response_chars": len(response.raw_text),
                "timeout_seconds": self.timeout_seconds,
                "max_output_tokens": self.max_output_tokens,
                "elapsed_ms": elapsed_ms,
                "reply_status": plan.reply_status,
                "usage": response.usage,
                **(response.response_details or {}),
            },
        )
        return plan, record

    def _render_prompt(
        self,
        *,
        message: EmailMessage,
        email_classification: EmailClassification,
    ) -> str:
        message_json = json.dumps(message.model_dump(mode="json"), sort_keys=True)
        classification_json = json.dumps(
            email_classification.model_dump(mode="json"),
            sort_keys=True,
        )
        return (
            f"{self.prompt.instructions.strip()}\n\n"
            "EMAIL_MESSAGE_DATA:\n"
            f"{message_json}\n\n"
            "EMAIL_CLASSIFICATION_JSON:\n"
            f"{classification_json}\n\n"
            "CLASSIFICATION_DISPLAY_CONTEXT:\n"
            f"Layer 1: {LEVEL1_DISPLAY_NAMES[email_classification.level1_classification]}\n"
            f"Layer 2: {LEVEL2_DISPLAY_NAMES[email_classification.level2_intent]}\n\n"
            "Return one JSON object that matches the reply-planning schema exactly.\n"
            "Do not wrap the answer in another object.\n"
            "Do not echo the full input objects back in the final answer."
        )


class ReplyPlanDraftWriterTool:
    """Convert a structured plan into an internal approval draft."""

    def __init__(
        self,
        *,
        tool_id: str,
        template_config: dict[str, Any],
        operator_email: str,
    ) -> None:
        self.tool_id = tool_id
        self.template_config = template_config
        self.operator_email = operator_email

    def write(
        self,
        *,
        message: EmailMessage,
        email_classification: EmailClassification,
        plan: ReplyPlan,
        draft_recipient: str | None = None,
    ) -> tuple[ReplyPlanningDraftPackage, ToolExecutionRecord]:
        template = str(
            self.template_config.get(
                "template",
                "Reply plan for {contact_name} ({contact_email})\n"
                "Original subject: {subject}\n"
                "Thread ID: {thread_id}\n"
                "Classification: {level1} / {level2}\n"
                "Reply status: {reply_status}\n"
                "Reply goal: {reply_goal}\n"
                "Confidence: {confidence}\n"
                "\nSuggested reply points:\n{reply_points}\n"
                "\nMissing information:\n{missing_information}\n"
                "\nSuggested tools:\n{suggested_tools}\n"
                "\nPlan steps:\n{plan_steps}\n"
                "\nWhy:\n{rationale}",
            )
        )
        draft = ReplyPlanningDraftPackage(
            message_id=message.message_id,
            to_email=draft_recipient or self.operator_email,
            draft_subject=f"Reply plan: {message.subject}",
            draft_body=template.format(
                contact_name=message.from_name or message.from_email,
                contact_email=message.from_email,
                subject=message.subject,
                thread_id=message.thread_id or "not available",
                level1=LEVEL1_DISPLAY_NAMES[email_classification.level1_classification],
                level2=LEVEL2_DISPLAY_NAMES[email_classification.level2_intent],
                reply_status=plan.reply_status,
                reply_goal=plan.reply_goal,
                confidence=f"{plan.confidence:.2f}",
                reply_points=_bullet_list(
                    plan.suggested_reply_points,
                    empty_text="- No reply points proposed yet.",
                ),
                missing_information=_bullet_list(
                    plan.missing_information,
                    empty_text="- No major missing information flagged.",
                ),
                suggested_tools=_tool_bullets(plan.suggested_tools),
                plan_steps=_bullet_list(
                    plan.plan_steps,
                    empty_text="- No extra planning steps required.",
                ),
                rationale=plan.rationale,
            ).strip(),
            thread_id=None,
            draft_created=False,
            draft_id=None,
        )
        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="reply_plan_draft_writer",
            status=ToolExecutionStatus.COMPLETED,
            details={"chars": len(draft.draft_body), "reply_status": plan.reply_status},
        )
        return draft, record


def _bullet_list(items: list[str], *, empty_text: str) -> str:
    cleaned = [item.strip() for item in items if item.strip()]
    if not cleaned:
        return empty_text
    return "\n".join(f"- {item}" for item in cleaned)


def _tool_bullets(suggestions: list[Any]) -> str:
    if not suggestions:
        return "- No extra tools needed right now."
    lines: list[str] = []
    for suggestion in suggestions:
        name = str(getattr(suggestion, "tool_name", "")).strip() or "unknown"
        why_needed = str(getattr(suggestion, "why_needed", "")).strip() or "No reason provided."
        lines.append(f"- {name}: {why_needed}")
    return "\n".join(lines)


def _resolve_timeout_seconds(
    tool_definition: WorkflowToolDefinition,
    settings: Settings,
) -> int:
    raw_value = tool_definition.config.get("timeout_seconds")
    if raw_value in {None, ""}:
        return int(settings.openai_timeout_seconds)
    try:
        timeout_seconds = int(str(raw_value))
    except (TypeError, ValueError):
        timeout_seconds = int(settings.openai_timeout_seconds)
    return max(1, min(timeout_seconds, 150))


def _resolve_max_output_tokens(
    tool_definition: WorkflowToolDefinition,
) -> int | None:
    raw_value = tool_definition.config.get("max_output_tokens")
    if raw_value in {None, ""}:
        return None
    try:
        parsed = int(str(raw_value))
    except (TypeError, ValueError):
        return None
    return max(1, parsed)
