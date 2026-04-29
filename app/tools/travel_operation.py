"""Structured planning and execution tools for governed travel operations."""

from __future__ import annotations

import base64
import json
from time import perf_counter
from typing import Any

from pydantic import BaseModel

from app.observability.langsmith import instrument_openai_client
from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.local_llm_classifier import (
    OpenAILLMClient,
    _extract_openai_payload,
    _extract_openai_usage,
    _openai_strict_json_schema,
    _response_schema_name,
)
from app.tools.models import ToolExecutionError, ToolExecutionRecord, ToolExecutionStatus
from app.workers.travel_operation_models import (
    TravelAttachmentText,
    TravelEmailDocument,
    TravelExecutionReview,
    TravelItineraryExtraction,
    TravelOperationPlan,
    TravelPlanCritique,
    TravelRouteResearchReport,
)


class TravelAttachmentOCRTool:
    """Extract bounded text from image attachments through OpenAI vision."""

    def __init__(
        self,
        *,
        tool_definition: WorkflowToolDefinition,
        prompt: PromptDocument,
        settings: Settings,
        client: Any | None = None,
    ) -> None:
        self.tool_definition = tool_definition
        self.prompt = prompt
        self.settings = settings
        self.provider = tool_definition.provider or "openai"
        self.model = tool_definition.model or "gpt-5.2"
        self.timeout_seconds = int(
            tool_definition.config.get("timeout_seconds", settings.openai_timeout_seconds)
        )
        self.max_output_tokens = int(tool_definition.config.get("max_output_tokens", 600))
        self.max_images = int(tool_definition.config.get("max_images", 3))
        if self.provider != "openai":
            raise ValueError("TravelAttachmentOCRTool currently supports only OpenAI.")
        self.client = client

    def extract_attachment_texts(
        self,
        *,
        message_id: str,
        attachments: list[dict[str, Any]],
    ) -> tuple[list[TravelAttachmentText], ToolExecutionRecord]:
        image_attachments = [
            attachment
            for attachment in attachments
            if str(attachment.get("mime_type", "")).lower().startswith("image/")
        ][: self.max_images]
        if not image_attachments:
            return [], ToolExecutionRecord(
                tool_id=self.tool_definition.tool_id,
                tool_kind=self.tool_definition.kind,
                status=ToolExecutionStatus.SKIPPED,
                details={"reason": "no_image_attachments", "message_id": message_id},
            )

        client = self.client or _build_openai_client(
            settings=self.settings,
            timeout_seconds=self.timeout_seconds,
            tool_definition=self.tool_definition,
        )
        extracted: list[TravelAttachmentText] = []
        usage_snapshots: list[dict[str, Any]] = []
        started = perf_counter()
        for attachment in image_attachments:
            mime_type = str(attachment.get("mime_type", "")).strip().lower()
            content = attachment.get("content")
            if not isinstance(content, bytes):
                continue
            filename = _optional_string(attachment.get("filename"))
            data_url = (
                f"data:{mime_type};base64,"
                f"{base64.b64encode(content).decode('ascii')}"
            )
            try:
                response = client.responses.create(
                    model=self.model,
                    instructions=self.prompt.instructions.strip(),
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": (
                                        "Extract the travel-relevant text from this attachment. "
                                        "Return plain text only."
                                    ),
                                },
                                {"type": "input_image", "image_url": data_url},
                            ],
                        }
                    ],
                    max_output_tokens=self.max_output_tokens,
                )
            except Exception as exc:
                raise ToolExecutionError(
                    f"OpenAI attachment OCR failed: {exc}",
                    tool_record=ToolExecutionRecord(
                        tool_id=self.tool_definition.tool_id,
                        tool_kind=self.tool_definition.kind,
                        status=ToolExecutionStatus.FAILED,
                        details={
                            "provider": self.provider,
                            "model": self.model,
                            "message_id": message_id,
                            "filename": filename,
                            "mime_type": mime_type,
                            "error": str(exc),
                        },
                    ),
                ) from exc
            text = str(getattr(response, "output_text", "") or "").strip()
            if not text:
                continue
            usage = _extract_openai_usage(response) or {}
            usage_snapshots.append(usage)
            extracted.append(
                TravelAttachmentText(
                    filename=filename,
                    mime_type=mime_type,
                    text=text[:12000],
                    extraction_method="image_ocr",
                )
            )

        elapsed_ms = int((perf_counter() - started) * 1000)
        return extracted, ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "provider": self.provider,
                "model": self.model,
                "message_id": message_id,
                "attachment_count": len(image_attachments),
                "ocr_text_count": len(extracted),
                "elapsed_ms": elapsed_ms,
                "usage": _merge_openai_usage(usage_snapshots),
            },
        )


class TravelPlanBuilderTool:
    """Build a structured travel-operation plan from one operator request email."""

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
        self.timeout_seconds = int(
            tool_definition.config.get("timeout_seconds", settings.openai_timeout_seconds)
        )
        self.max_output_tokens = int(tool_definition.config.get("max_output_tokens", 1500))
        self.client = OpenAILLMClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout_seconds=self.timeout_seconds,
            settings=settings,
            tracing_name=tool_definition.tool_id,
            tracing_metadata={
                "tool_id": tool_definition.tool_id,
                "tool_kind": tool_definition.kind,
                "provider": self.provider,
            },
        )

    def build(
        self,
        *,
        request_document: TravelEmailDocument,
    ) -> tuple[TravelOperationPlan, ToolExecutionRecord]:
        return _run_structured_openai_tool(
            client=self.client,
            tool_definition=self.tool_definition,
            prompt=self.prompt,
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            response_model=TravelOperationPlan,
            input_sections={
                "REQUEST_EMAIL_JSON": json.dumps(
                    _travel_email_payload(request_document),
                    indent=2,
                    sort_keys=True,
                )
            },
        )


class TravelPlanCriticTool:
    """Critique a proposed travel-operation plan before any execution happens."""

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
        self.timeout_seconds = int(
            tool_definition.config.get("timeout_seconds", settings.openai_timeout_seconds)
        )
        self.max_output_tokens = int(tool_definition.config.get("max_output_tokens", 900))
        self.client = OpenAILLMClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout_seconds=self.timeout_seconds,
            settings=settings,
            tracing_name=tool_definition.tool_id,
            tracing_metadata={
                "tool_id": tool_definition.tool_id,
                "tool_kind": tool_definition.kind,
                "provider": self.provider,
            },
        )

    def critique(
        self,
        *,
        request_document: TravelEmailDocument,
        plan: TravelOperationPlan,
    ) -> tuple[TravelPlanCritique, ToolExecutionRecord]:
        return _run_structured_openai_tool(
            client=self.client,
            tool_definition=self.tool_definition,
            prompt=self.prompt,
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            response_model=TravelPlanCritique,
            input_sections={
                "REQUEST_EMAIL_JSON": json.dumps(
                    _travel_email_payload(request_document),
                    indent=2,
                    sort_keys=True,
                ),
                "PROPOSED_PLAN_JSON": plan.model_dump_json(indent=2),
            },
        )


class TravelPlanReviserTool:
    """Revise a travel-operation plan against a structured critique."""

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
        self.timeout_seconds = int(
            tool_definition.config.get("timeout_seconds", settings.openai_timeout_seconds)
        )
        self.max_output_tokens = int(tool_definition.config.get("max_output_tokens", 1500))
        self.client = OpenAILLMClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout_seconds=self.timeout_seconds,
            settings=settings,
            tracing_name=tool_definition.tool_id,
            tracing_metadata={
                "tool_id": tool_definition.tool_id,
                "tool_kind": tool_definition.kind,
                "provider": self.provider,
            },
        )

    def revise(
        self,
        *,
        request_document: TravelEmailDocument,
        plan: TravelOperationPlan,
        critique: TravelPlanCritique,
    ) -> tuple[TravelOperationPlan, ToolExecutionRecord]:
        return _run_structured_openai_tool(
            client=self.client,
            tool_definition=self.tool_definition,
            prompt=self.prompt,
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            response_model=TravelOperationPlan,
            input_sections={
                "REQUEST_EMAIL_JSON": json.dumps(
                    _travel_email_payload(request_document),
                    indent=2,
                    sort_keys=True,
                ),
                "PROPOSED_PLAN_JSON": plan.model_dump_json(indent=2),
                "CRITIQUE_JSON": critique.model_dump_json(indent=2),
            },
        )


class TravelItineraryExtractorTool:
    """Extract the final booked itinerary from a concrete booking email."""

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
        self.timeout_seconds = int(
            tool_definition.config.get("timeout_seconds", settings.openai_timeout_seconds)
        )
        self.max_output_tokens = int(tool_definition.config.get("max_output_tokens", 1200))
        self.client = OpenAILLMClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout_seconds=self.timeout_seconds,
            settings=settings,
            tracing_name=tool_definition.tool_id,
            tracing_metadata={
                "tool_id": tool_definition.tool_id,
                "tool_kind": tool_definition.kind,
                "provider": self.provider,
            },
        )

    def extract(
        self,
        *,
        booking_document: TravelEmailDocument,
    ) -> tuple[TravelItineraryExtraction, ToolExecutionRecord]:
        return _run_structured_openai_tool(
            client=self.client,
            tool_definition=self.tool_definition,
            prompt=self.prompt,
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            response_model=TravelItineraryExtraction,
            input_sections={
                "BOOKING_EMAIL_JSON": json.dumps(
                    _travel_email_payload(booking_document),
                    indent=2,
                    sort_keys=True,
                )
            },
        )


class TravelRouteResearchTool:
    """Use OpenAI web search to research airport travel and security buffers."""

    def __init__(
        self,
        *,
        tool_definition: WorkflowToolDefinition,
        prompt: PromptDocument,
        settings: Settings,
        client: Any | None = None,
    ) -> None:
        self.tool_definition = tool_definition
        self.prompt = prompt
        self.settings = settings
        self.provider = tool_definition.provider or "openai"
        self.model = tool_definition.model or "gpt-5.2"
        self.timeout_seconds = int(
            tool_definition.config.get("timeout_seconds", settings.openai_timeout_seconds)
        )
        self.max_output_tokens = int(tool_definition.config.get("max_output_tokens", 1000))
        self.search_context_size = (
            str(tool_definition.config.get("search_context_size", "medium")).strip() or "medium"
        )
        if self.provider != "openai":
            raise ValueError("TravelRouteResearchTool currently supports only OpenAI.")
        self.client = client

    def research(
        self,
        *,
        origin: str,
        destination: str,
        departure_local_iso: str,
        airport_name: str,
        airport_code: str | None,
    ) -> tuple[TravelRouteResearchReport, ToolExecutionRecord]:
        client = self.client or _build_openai_client(
            settings=self.settings,
            timeout_seconds=self.timeout_seconds,
            tool_definition=self.tool_definition,
        )
        strict_schema = _openai_strict_json_schema(
            TravelRouteResearchReport.model_json_schema()
        )
        rendered_input = (
            "TRAVEL_REQUEST_JSON:\n"
            f"{json.dumps(
                {
                    'origin': origin,
                    'destination': destination,
                    'departure_local_iso': departure_local_iso,
                    'airport_name': airport_name,
                    'airport_code': airport_code,
                },
                indent=2,
                sort_keys=True,
            )}\n"
        )
        started = perf_counter()
        try:
            response = client.responses.create(
                model=self.model,
                instructions=self.prompt.instructions.strip(),
                input=rendered_input,
                max_output_tokens=self.max_output_tokens,
                max_tool_calls=1,
                parallel_tool_calls=False,
                include=["web_search_call.action.sources"],
                tools=[
                    {
                        "type": "web_search",
                        "search_context_size": self.search_context_size,
                        "user_location": {
                            "type": "approximate",
                            "country": "FR",
                            "timezone": "Europe/Paris",
                        },
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": _response_schema_name(strict_schema),
                        "schema": strict_schema,
                        "strict": True,
                    }
                },
            )
        except Exception as exc:
            raise ToolExecutionError(
                f"OpenAI route research failed: {exc}",
                tool_record=ToolExecutionRecord(
                    tool_id=self.tool_definition.tool_id,
                    tool_kind=self.tool_definition.kind,
                    status=ToolExecutionStatus.FAILED,
                    details={"provider": self.provider, "model": self.model, "error": str(exc)},
                ),
            ) from exc
        payload = _extract_openai_payload(
            response=response,
            raw_text=str(getattr(response, "output_text", "") or "").strip(),
            response_model=TravelRouteResearchReport,
        )
        report = TravelRouteResearchReport.model_validate(payload)
        elapsed_ms = int((perf_counter() - started) * 1000)
        return report, ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "provider": self.provider,
                "model": self.model,
                "origin": origin,
                "destination": destination,
                "elapsed_ms": elapsed_ms,
                "usage": _extract_openai_usage(response),
            },
        )


class TravelExecutionReviewerTool:
    """Use Gemini to review whether the approved travel plan was executed well."""

    def __init__(
        self,
        *,
        tool_definition: WorkflowToolDefinition,
        prompt: PromptDocument,
        settings: Settings,
        client: Any | None = None,
    ) -> None:
        self.tool_definition = tool_definition
        self.prompt = prompt
        self.settings = settings
        self.provider = tool_definition.provider or "gemini"
        self.model = tool_definition.model or "gemini-2.5-pro"
        if self.provider != "gemini":
            raise ValueError("TravelExecutionReviewerTool currently supports only Gemini.")
        self.client = client

    def review(
        self,
        *,
        plan: TravelOperationPlan,
        itinerary: TravelItineraryExtraction | None,
        route_report: TravelRouteResearchReport | None,
        execution_summary: dict[str, Any],
    ) -> tuple[TravelExecutionReview, ToolExecutionRecord]:
        client = self.client or _build_gemini_client(self.settings)
        prompt_text = (
            f"{self.prompt.instructions.strip()}\n\n"
            "APPROVED_PLAN_JSON:\n"
            f"{plan.model_dump_json(indent=2)}\n\n"
            "ITINERARY_JSON:\n"
            f"{json.dumps(
                itinerary.model_dump(mode='json') if itinerary is not None else None,
                indent=2,
                sort_keys=True,
            )}\n\n"
            "ROUTE_REPORT_JSON:\n"
            f"{json.dumps(
                route_report.model_dump(mode='json') if route_report is not None else None,
                indent=2,
                sort_keys=True,
            )}\n\n"
            "EXECUTION_SUMMARY_JSON:\n"
            f"{json.dumps(execution_summary, indent=2, sort_keys=True)}\n"
        )
        started = perf_counter()
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=prompt_text,
            )
        except Exception as exc:
            raise ToolExecutionError(
                f"Gemini execution review failed: {exc}",
                tool_record=ToolExecutionRecord(
                    tool_id=self.tool_definition.tool_id,
                    tool_kind=self.tool_definition.kind,
                    status=ToolExecutionStatus.FAILED,
                    details={"provider": self.provider, "model": self.model, "error": str(exc)},
                ),
            ) from exc
        raw_text = str(getattr(response, "text", "") or "").strip()
        if not raw_text:
            raise ToolExecutionError(
                "Gemini execution review returned no text.",
                tool_record=ToolExecutionRecord(
                    tool_id=self.tool_definition.tool_id,
                    tool_kind=self.tool_definition.kind,
                    status=ToolExecutionStatus.FAILED,
                    details={"provider": self.provider, "model": self.model},
                ),
            )
        try:
            review = _parse_travel_execution_review(raw_text)
        except Exception as exc:
            raise ToolExecutionError(
                f"Gemini execution review returned invalid JSON: {exc}",
                tool_record=ToolExecutionRecord(
                    tool_id=self.tool_definition.tool_id,
                    tool_kind=self.tool_definition.kind,
                    status=ToolExecutionStatus.FAILED,
                    details={
                        "provider": self.provider,
                        "model": self.model,
                        "error": str(exc),
                        "raw_text": raw_text[:1200],
                    },
                ),
            ) from exc
        elapsed_ms = int((perf_counter() - started) * 1000)
        return review, ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "provider": self.provider,
                "model": self.model,
                "elapsed_ms": elapsed_ms,
                "verdict": review.verdict,
            },
        )


def _parse_travel_execution_review(raw_text: str) -> TravelExecutionReview:
    cleaned = _strip_markdown_code_fence(raw_text)
    try:
        return TravelExecutionReview.model_validate_json(cleaned)
    except Exception:
        return TravelExecutionReview.model_validate(_extract_json_object(cleaned))


def _strip_markdown_code_fence(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = _strip_markdown_code_fence(raw_text)

    decoder = json.JSONDecoder()
    try:
        parsed = decoder.decode(cleaned)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    for start in (index for index, char in enumerate(cleaned) if char == "{"):
        try:
            candidate, _ = decoder.raw_decode(cleaned[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate

    raise ValueError("Gemini review did not contain a valid JSON object")


def _run_structured_openai_tool(
    *,
    client: OpenAILLMClient,
    tool_definition: WorkflowToolDefinition,
    prompt: PromptDocument,
    model: str,
    max_output_tokens: int,
    response_model: type[BaseModel],
    input_sections: dict[str, str],
) -> tuple[Any, ToolExecutionRecord]:
    prompt_text = prompt.instructions.strip()
    for key, value in input_sections.items():
        prompt_text += f"\n\n{key}:\n{value.strip()}\n"
    prompt_text += (
        "\nReturn strict JSON only.\n"
        "Do not include markdown fences or prose outside the JSON object."
    )
    try:
        response = client.classify(
            prompt=prompt_text,
            model=model,
            response_schema=response_model.model_json_schema(),
            response_model=response_model,
            max_output_tokens=max_output_tokens,
        )
    except Exception as exc:
        raise ToolExecutionError(
            f"{tool_definition.kind} failed: {exc}",
            tool_record=ToolExecutionRecord(
                tool_id=tool_definition.tool_id,
                tool_kind=tool_definition.kind,
                status=ToolExecutionStatus.FAILED,
                details={
                    "provider": tool_definition.provider,
                    "model": model,
                    "error": str(exc),
                    "max_output_tokens": max_output_tokens,
                },
            ),
        ) from exc
    payload = response_model.model_validate(response.payload)
    record = ToolExecutionRecord(
        tool_id=tool_definition.tool_id,
        tool_kind=tool_definition.kind,
        status=ToolExecutionStatus.COMPLETED,
        details={
            "provider": tool_definition.provider,
            "model": model,
            "usage": response.usage or {},
            "response_details": response.response_details or {},
            "max_output_tokens": max_output_tokens,
        },
    )
    return payload, record


def _build_openai_client(
    *,
    settings: Settings,
    timeout_seconds: int,
    tool_definition: WorkflowToolDefinition,
) -> Any:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("The openai package is not installed.") from exc
    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=(settings.openai_base_url or "https://api.openai.com/v1").rstrip("/"),
        timeout=timeout_seconds,
    )
    return instrument_openai_client(
        client,
        settings=settings,
        run_name=tool_definition.tool_id,
        metadata={
            "tool_id": tool_definition.tool_id,
            "tool_kind": tool_definition.kind,
            "provider": tool_definition.provider or "openai",
        },
    )


def _build_gemini_client(settings: Settings) -> Any:
    if not settings.gemini_api_key:
        raise RuntimeError("SAI_GEMINI_API_KEY is not configured.")
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("The google-genai package is not installed.") from exc
    return genai.Client(api_key=settings.gemini_api_key)


def _travel_email_payload(document: TravelEmailDocument) -> dict[str, Any]:
    return {
        "message_id": document.message.message_id,
        "thread_id": document.message.thread_id,
        "from_email": document.message.from_email,
        "from_name": document.message.from_name,
        "to": document.message.to,
        "cc": document.message.cc,
        "subject": document.message.subject,
        "snippet": document.message.snippet,
        "received_at": (
            document.message.received_at.isoformat()
            if document.message.received_at is not None
            else None
        ),
        "plain_text": document.plain_text[:12000],
        "html_text": document.html_text[:12000],
        "attachment_texts": [item.model_dump(mode="json") for item in document.attachment_texts],
    }


def _merge_openai_usage(usages: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for usage in usages:
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            raw_value = usage.get(key)
            if isinstance(raw_value, int):
                totals[key] = totals.get(key, 0) + raw_value
    return totals


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
