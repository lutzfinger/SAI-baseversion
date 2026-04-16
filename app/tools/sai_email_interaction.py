"""Cloud-first planning helpers for the starter email-native task lane."""

from __future__ import annotations

import json
from time import perf_counter

from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.local_llm_classifier import (
    OpenAIJSONClient,
    StructuredLLMResponse,
    llm_exception_details,
)
from app.tools.models import ToolExecutionError, ToolExecutionRecord, ToolExecutionStatus
from app.workers.sai_email_interaction_models import SaiEmailGenericPlan


class SaiEmailGenericPlannerTool:
    """Plan one operator email into a question, answer, or approval-backed proposal."""

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
        self.provider = (tool_definition.provider or "openai").strip().lower()
        self.model = tool_definition.model or "gpt-5.2"
        self.timeout_seconds = _resolve_timeout_seconds(tool_definition, settings)
        self.max_output_tokens = _resolve_max_output_tokens(tool_definition)

    def plan(
        self,
        *,
        thread_id: str,
        request_message_id: str,
        request_text: str,
        thread_state_summary: dict[str, object],
        task_context_summary: dict[str, object],
        known_facts: list[dict[str, object]],
        read_only_context: dict[str, object],
        workflow_catalog: list[dict[str, object]],
    ) -> tuple[SaiEmailGenericPlan, ToolExecutionRecord]:
        prompt_text = self._render_prompt(
            thread_id=thread_id,
            request_message_id=request_message_id,
            request_text=request_text,
            thread_state_summary=thread_state_summary,
            task_context_summary=task_context_summary,
            known_facts=known_facts,
            read_only_context=read_only_context,
            workflow_catalog=workflow_catalog,
        )
        started = perf_counter()
        try:
            response = self._client().classify(
                prompt=prompt_text,
                model=self.model,
                response_schema=SaiEmailGenericPlan.model_json_schema(),
                response_model=SaiEmailGenericPlan,
                max_output_tokens=self.max_output_tokens,
            )
            plan = SaiEmailGenericPlan.model_validate(response.payload)
            elapsed_ms = int((perf_counter() - started) * 1000)
        except Exception as exc:
            elapsed_ms = int((perf_counter() - started) * 1000)
            record = ToolExecutionRecord(
                tool_id=self.tool_definition.tool_id,
                tool_kind=self.tool_definition.kind,
                status=ToolExecutionStatus.FAILED,
                details={
                    "provider": self.provider,
                    "model": self.model,
                    "elapsed_ms": elapsed_ms,
                    **llm_exception_details(exc),
                },
            )
            raise ToolExecutionError(
                f"Starter email planner failed: {exc}",
                tool_record=record,
                context={"thread_id": thread_id, "request_message_id": request_message_id},
            ) from exc

        return plan, ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "provider": self.provider,
                "model": self.model,
                "elapsed_ms": elapsed_ms,
                "response_mode": plan.response_mode,
                "request_kind": plan.request_kind,
                "activity_count": len(plan.activities),
            },
        )

    def _client(self) -> OpenAIJSONClient:
        if self.provider == "mock":
            return _MockPlannerClient()
        if not self.settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for starter email planning.")
        return OpenAIJSONClient(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_base_url,
            timeout_seconds=self.timeout_seconds,
        )

    def _render_prompt(
        self,
        *,
        thread_id: str,
        request_message_id: str,
        request_text: str,
        thread_state_summary: dict[str, object],
        task_context_summary: dict[str, object],
        known_facts: list[dict[str, object]],
        read_only_context: dict[str, object],
        workflow_catalog: list[dict[str, object]],
    ) -> str:
        request_json = json.dumps(
            {
                "thread_id": thread_id,
                "request_message_id": request_message_id,
                "request_text": request_text,
                "thread_state_summary": thread_state_summary,
                "task_context_summary": task_context_summary,
                "known_facts": known_facts,
                "read_only_context": read_only_context,
            },
            sort_keys=True,
        )
        catalog_json = json.dumps({"workflows": workflow_catalog}, sort_keys=True)
        return (
            f"{self.prompt.instructions.strip()}\n\n"
            "EMAIL_REQUEST_JSON:\n"
            f"{request_json}\n\n"
            "WORKFLOW_CATALOG_JSON:\n"
            f"{catalog_json}\n\n"
            "Return one JSON object that matches the schema exactly.\n"
            "Favor assistive planning over autonomous action.\n"
            "If a workflow run or Slack post would write externally, package it inside "
            "execution_plan and set response_mode=ask_approval.\n"
        )


def compact_task_plan_for_email(plan: SaiEmailGenericPlan) -> str:
    return " ".join((plan.short_response or "").split())


def _resolve_timeout_seconds(tool_definition: WorkflowToolDefinition, settings: Settings) -> int:
    raw_value = tool_definition.config.get("timeout_seconds")
    if raw_value in {None, ""}:
        return settings.openai_timeout_seconds
    try:
        return max(5, int(raw_value))
    except (TypeError, ValueError):
        return settings.openai_timeout_seconds


def _resolve_max_output_tokens(tool_definition: WorkflowToolDefinition) -> int:
    raw_value = tool_definition.config.get("max_output_tokens")
    if raw_value in {None, ""}:
        return 700
    try:
        return max(200, int(raw_value))
    except (TypeError, ValueError):
        return 700


class _MockPlannerClient(OpenAIJSONClient):
    def __init__(self) -> None:
        pass

    def classify(
        self,
        *,
        prompt: str,
        model: str,
        response_schema: dict[str, object],
        response_model: type[object] | None = None,
        max_output_tokens: int | None = None,
    ) -> StructuredLLMResponse:
        del model, response_schema, response_model, max_output_tokens
        response = {
            "response_mode": "ask_approval",
            "short_response": (
                "I can run the newsletter tagging workflow for this inbox slice. Approve?"
            ),
            "explanation": (
                "The request maps cleanly to a supported starter workflow, but it "
                "writes Gmail labels, so I am asking before executing it."
            ),
            "activities": [
                {
                    "activity_id": "1",
                    "activity_kind": "plan",
                    "description": "Mapped the request to a starter workflow.",
                    "approval_required": True,
                }
            ],
            "request_kind": "workflow_suggestion",
            "execution_plan": {
                "task_summary": "Run a starter workflow",
                "approach_summary": "Execute the most relevant starter workflow after approval.",
                "operator_approval_question": "Approve the starter workflow execution?",
                "requires_approval": True,
                "risk_level": "moderate",
                "actions": [
                    {
                        "action_id": "run-1",
                        "action_kind": "run_workflow",
                        "purpose": "Run the starter newsletter tagging workflow.",
                        "workflow_id": "newsletter-identification-gmail-tagging",
                        "connector_overrides": {},
                    }
                ],
                "confidence": 0.7,
                "rationale": "The request fits an existing starter workflow.",
                "safety_notes": ["Approval is required before Gmail labels are modified."],
            },
            "follow_up_question": None,
        }
        raw_text = json.dumps(response, sort_keys=True)
        return StructuredLLMResponse(payload=response, raw_text=raw_text)
