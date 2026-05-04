"""Structured starter email classification with local and cloud LLMs."""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any

from pydantic import BaseModel

from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.langchain_client import (
    LangChainStructuredClient,
    StructuredLLMResponse,
    llm_exception_details,
)
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import EmailClassification


class MockJSONClient:
    """Deterministic fallback client used by tests and dry local setups."""

    def classify(
        self,
        *,
        prompt: str,
        model: str,
        response_schema: dict[str, Any],
        response_model: type[BaseModel] | None = None,
        max_output_tokens: int | None = None,
    ) -> StructuredLLMResponse:
        del model, response_schema, max_output_tokens
        payload = {
            "message_id": _extract_message_id(prompt),
            "level1_classification": "newsletters"
            if "unsubscribe" in prompt.lower() or "newsletter" in prompt.lower()
            else "no_label",
            "level2_intent": "informational",
            "confidence": 0.66,
            "reason": "Mock classifier inferred the category from the prompt content.",
        }
        if response_model is not None:
            payload = response_model.model_validate(payload).model_dump(mode="json")
        return StructuredLLMResponse(payload=payload, raw_text=json.dumps(payload, sort_keys=True))


class StructuredEmailClassifierTool:
    """Starter email classifier backed by a workflow-declared model surface."""

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
        self.model = tool_definition.model or (
            settings.local_llm_model
            if tool_definition.kind == "local_llm_classifier"
            else "gpt-5.2"
        )
        self.timeout_seconds = _resolve_timeout_seconds(tool_definition, settings)
        self.max_output_tokens = _resolve_max_output_tokens(tool_definition)
        self.client = self._build_client()

    def classify(
        self,
        *,
        message_payload: dict[str, Any],
        operator_email: str,
        keyword_baseline: EmailClassification | dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, ToolExecutionRecord]:
        if (
            self.tool_definition.kind == "local_llm_classifier"
            and not self.settings.local_llm_enabled
        ):
            return None, ToolExecutionRecord(
                tool_id=self.tool_definition.tool_id,
                tool_kind=self.tool_definition.kind,
                status=ToolExecutionStatus.SKIPPED,
                details={"reason": "local_llm_disabled"},
            )
        if self.client is None:
            return None, ToolExecutionRecord(
                tool_id=self.tool_definition.tool_id,
                tool_kind=self.tool_definition.kind,
                status=ToolExecutionStatus.SKIPPED,
                details={"reason": "llm_client_unavailable", "provider": self.provider},
            )
        prompt_text = self._render_prompt(
            message_payload=message_payload,
            operator_email=operator_email,
            keyword_baseline=keyword_baseline,
        )
        started = perf_counter()
        try:
            response = self.client.classify(
                prompt=prompt_text,
                model=self.model,
                response_schema=EmailClassification.model_json_schema(),
                response_model=EmailClassification,
                max_output_tokens=self.max_output_tokens,
            )
            elapsed_ms = int((perf_counter() - started) * 1000)
            return response.payload, ToolExecutionRecord(
                tool_id=self.tool_definition.tool_id,
                tool_kind=self.tool_definition.kind,
                status=ToolExecutionStatus.COMPLETED,
                details={
                    "provider": self.provider,
                    "model": self.model,
                    "elapsed_ms": elapsed_ms,
                    "usage": response.usage,
                    **(response.response_details or {}),
                },
            )
        except Exception as exc:
            elapsed_ms = int((perf_counter() - started) * 1000)
            return None, ToolExecutionRecord(
                tool_id=self.tool_definition.tool_id,
                tool_kind=self.tool_definition.kind,
                status=ToolExecutionStatus.FALLBACK,
                details={
                    "provider": self.provider,
                    "model": self.model,
                    "elapsed_ms": elapsed_ms,
                    **llm_exception_details(exc),
                },
            )

    def _build_client(self) -> LangChainStructuredClient | MockJSONClient | None:
        if self.provider == "mock":
            return MockJSONClient()
        if self.tool_definition.kind == "local_llm_classifier":
            return LangChainStructuredClient(
                provider="ollama",
                model=self.model,
                settings=self.settings,
                timeout_seconds=self.timeout_seconds,
                max_output_tokens=self.max_output_tokens,
                run_name=self.tool_definition.tool_id,
                run_tags=[self.tool_definition.kind, "provider:ollama"],
                run_metadata={
                    "tool_id": self.tool_definition.tool_id,
                    "tool_kind": self.tool_definition.kind,
                    "environment": self.settings.environment,
                },
            )
        if self.provider == "openai":
            if not self.settings.openai_api_key:
                return None
            return LangChainStructuredClient(
                provider="openai",
                model=self.model,
                settings=self.settings,
                timeout_seconds=self.timeout_seconds,
                max_output_tokens=self.max_output_tokens,
                run_name=self.tool_definition.tool_id,
                run_tags=[self.tool_definition.kind, "provider:openai"],
                run_metadata={
                    "tool_id": self.tool_definition.tool_id,
                    "tool_kind": self.tool_definition.kind,
                    "environment": self.settings.environment,
                },
            )
        return None

    def _render_prompt(
        self,
        *,
        message_payload: dict[str, Any],
        operator_email: str,
        keyword_baseline: EmailClassification | dict[str, Any] | None,
    ) -> str:
        baseline_section = ""
        if keyword_baseline is not None:
            if isinstance(keyword_baseline, EmailClassification):
                baseline_payload = keyword_baseline.model_dump(mode="json")
            else:
                baseline_payload = keyword_baseline
            baseline_section = (
                f"KEYWORD_BASELINE_JSON:\n{json.dumps(baseline_payload, sort_keys=True)}\n\n"
            )
        return (
            f"{self.prompt.instructions.strip()}\n\n"
            f"OPERATOR_EMAIL: {operator_email}\n"
            "EMAIL_MESSAGE_JSON:\n"
            f"{json.dumps(message_payload, sort_keys=True)}\n\n"
            f"{baseline_section}"
            "Return one JSON object that matches the schema exactly.\n"
            "Use `newsletter` only for recurring, list-like or broadcast-style mail.\n"
            "Use `general` for ordinary human communication.\n"
            "Use `other` only when neither label is supportable.\n"
        )


def _resolve_timeout_seconds(tool_definition: WorkflowToolDefinition, settings: Settings) -> int:
    raw_value = tool_definition.config.get("timeout_seconds")
    if raw_value in {None, ""}:
        if tool_definition.kind == "local_llm_classifier":
            return settings.local_llm_timeout_seconds
        return settings.openai_timeout_seconds
    if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int, float)):
        return settings.openai_timeout_seconds
    try:
        return max(5, int(raw_value))
    except (TypeError, ValueError):
        return settings.openai_timeout_seconds


def _resolve_max_output_tokens(tool_definition: WorkflowToolDefinition) -> int:
    raw_value = tool_definition.config.get("max_output_tokens")
    if raw_value in {None, ""}:
        return 300
    if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int, float)):
        return 300
    try:
        return max(100, int(raw_value))
    except (TypeError, ValueError):
        return 300


def _extract_message_id(prompt: str) -> str:
    marker = '"message_id": "'
    if marker not in prompt:
        return "unknown"
    return prompt.split(marker, 1)[1].split('"', 1)[0]
