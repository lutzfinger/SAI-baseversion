"""Weekly people-of-interest research using OpenAI web search."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

from app.learning.people_of_interest_registry import PersonOfInterest
from app.observability.langsmith import instrument_openai_client
from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.local_llm_classifier import (
    _extract_openai_payload,
    _extract_openai_usage,
    _openai_strict_json_schema,
    _response_schema_name,
    _safe_model_dump_json,
)
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.people_of_interest_models import PersonOfInterestSearchResponse


class PeopleOfInterestResearchTool:
    """Research one monitored person with OpenAI web search."""

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
        self.max_output_tokens = int(tool_definition.config.get("max_output_tokens", 900))
        self.search_context_size = (
            str(tool_definition.config.get("search_context_size", "medium")).strip() or "medium"
        )
        if self.provider != "openai":
            raise ValueError("PeopleOfInterestResearchTool currently supports only OpenAI.")
        self.client = client

    def research(
        self,
        *,
        person: PersonOfInterest,
    ) -> tuple[PersonOfInterestSearchResponse, ToolExecutionRecord]:
        client = self.client or _build_openai_client(
            settings=self.settings,
            timeout_seconds=self.timeout_seconds,
            tool_definition=self.tool_definition,
        )
        rendered_input = _render_search_input(person)
        strict_schema = _openai_strict_json_schema(
            PersonOfInterestSearchResponse.model_json_schema()
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
                            "country": "US",
                            "region": "California",
                            "timezone": "America/Los_Angeles",
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
            raise RuntimeError(f"OpenAI people-of-interest search failed: {exc}") from exc
        elapsed_ms = int((perf_counter() - started) * 1000)
        raw_text = str(getattr(response, "output_text", "")).strip()
        payload = _extract_openai_payload(
            response=response,
            raw_text=raw_text,
            response_model=PersonOfInterestSearchResponse,
        )
        report = PersonOfInterestSearchResponse.model_validate(payload)
        response_payload = _safe_model_dump_json(response)
        sources = _extract_web_search_sources(response_payload)
        if not report.no_notable_updates and not sources:
            raise ValueError("OpenAI people-of-interest search returned no sources.")
        record = ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "provider": self.provider,
                "model": self.model,
                "person_id": person.person_id,
                "display_name": person.display_name,
                "elapsed_ms": elapsed_ms,
                "finding_count": len(report.findings),
                "no_notable_updates": report.no_notable_updates,
                "identity_confidence": report.identity_confidence,
                "source_count": len(sources),
                "usage": _extract_openai_usage(response),
            },
        )
        return report, record


def _build_openai_client(
    *,
    settings: Settings,
    timeout_seconds: int,
    tool_definition: WorkflowToolDefinition,
) -> Any:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured for OpenAI web search.")
    try:
        from openai import OpenAI
    except ImportError as exc:
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


def _render_search_input(person: PersonOfInterest) -> str:
    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    domain = urlparse(person.canonical_url).netloc or person.canonical_url
    identity_payload = {
        "person_id": person.person_id,
        "display_name": person.display_name,
        "canonical_url": person.canonical_url,
        "canonical_domain": domain,
        "organization": person.organization,
        "aliases": person.aliases,
        "notes": person.notes,
        "window_start_utc": week_ago.isoformat(),
        "window_end_utc": now.isoformat(),
    }
    return (
        "Research this person of interest for notable public updates from the last 7 days.\n"
        "Use the identity payload to avoid confusing them with similarly named people.\n"
        "Prefer substantive professional developments over generic mentions.\n\n"
        f"PERSON_IDENTITY_JSON:\n{json.dumps(identity_payload, indent=2, sort_keys=True)}\n"
    )


def _extract_web_search_sources(payload: object) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    _collect_sources(payload, collected)
    unique: dict[str, dict[str, Any]] = {}
    for item in collected:
        url = str(item.get("url", "")).strip()
        if url and url not in unique:
            unique[url] = item
    return list(unique.values())


def _collect_sources(payload: object, collected: list[dict[str, Any]]) -> None:
    if isinstance(payload, dict):
        sources = payload.get("sources")
        if isinstance(sources, list):
            for item in sources:
                if isinstance(item, dict):
                    collected.append(item)
        for value in payload.values():
            _collect_sources(value, collected)
        return
    if isinstance(payload, list):
        for value in payload:
            _collect_sources(value, collected)
