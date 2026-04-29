"""Guarded safe-for-work joke generation backed by OpenAI."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.local_llm_classifier import OpenAILLMClient
from app.tools.models import ToolExecutionError, ToolExecutionRecord, ToolExecutionStatus


class SafeJokeResponse(BaseModel):
    """One narrow SFW joke response returned to Slack."""

    model_config = ConfigDict(extra="forbid")

    request_summary: str
    joke_text: str = Field(min_length=1)
    safe_for_work: bool = True
    content_rating: Literal["g", "pg"] = "g"


class SafeJokeWriterTool:
    """Generate one safe-for-work joke from a bounded Slack request."""

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
        self.model = tool_definition.model or "gpt-5.2-chat-latest"
        self.timeout_seconds = max(
            15,
            int(tool_definition.config.get("timeout_seconds", settings.openai_timeout_seconds)),
        )
        self.max_output_tokens = max(
            120,
            int(tool_definition.config.get("max_output_tokens", 220)),
        )
        if self.provider != "openai":
            raise ValueError("SafeJokeWriterTool currently supports only the OpenAI provider")
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

    def generate(self, *, request_text: str) -> tuple[SafeJokeResponse, ToolExecutionRecord]:
        prompt_text = self._render_prompt(request_text=request_text)
        try:
            response = self.client.classify(
                prompt=prompt_text,
                model=self.model,
                response_schema=SafeJokeResponse.model_json_schema(),
                response_model=SafeJokeResponse,
                max_output_tokens=self.max_output_tokens,
            )
            joke = SafeJokeResponse.model_validate(response.payload)
        except Exception as exc:
            error_text = str(exc).strip() or exc.__class__.__name__
            raise ToolExecutionError(
                f"OpenAI safe joke generation failed: {error_text}",
                tool_record=ToolExecutionRecord(
                    tool_id=self.tool_definition.tool_id,
                    tool_kind=self.tool_definition.kind,
                    status=ToolExecutionStatus.FAILED,
                    details={
                        "provider": self.provider,
                        "model": self.model,
                        "error": error_text,
                        "request_chars": len(request_text),
                        "timeout_seconds": self.timeout_seconds,
                        "max_output_tokens": self.max_output_tokens,
                    },
                ),
                context={"item_id": "slack_joke_generation"},
            ) from exc

        record = ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "provider": self.provider,
                "model": self.model,
                "request_chars": len(request_text),
                "timeout_seconds": self.timeout_seconds,
                "max_output_tokens": self.max_output_tokens,
                "usage": response.usage,
                "response_details": response.response_details,
                "safe_for_work": joke.safe_for_work,
                "content_rating": joke.content_rating,
            },
        )
        return joke, record

    def _render_prompt(self, *, request_text: str) -> str:
        return (
            f"{self.prompt.instructions.strip()}\n\n"
            "SLACK_REQUEST_TEXT:\n"
            f"{request_text.strip()}\n\n"
            "Return one JSON object that matches the schema exactly.\n"
            "Do not include markdown fences or prose outside the JSON object."
        )


_SAFE_FALLBACK_JOKES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("calendar", "meeting", "schedule"),
        "Why did the calendar stay calm? It knew its days were numbered, but already blocked.",
    ),
    (
        ("flight", "travel", "airport"),
        "Why did the suitcase ace the meeting? It always came with good carry-on points.",
    ),
    (
        ("spreadsheet", "excel", "finance"),
        "Why was the spreadsheet so relaxed? It finally found the right balance sheet.",
    ),
    (
        ("email", "inbox", "gmail"),
        "Why did the inbox go to therapy? It had too many unresolved threads.",
    ),
    (
        ("coffee", "espresso", "cafe"),
        "Why did the coffee file a report? It felt mugged before the morning stand-up.",
    ),
)

_GENERIC_FALLBACK_JOKES: tuple[str, ...] = (
    "Why did the keyboard break up with the mouse? It felt constantly clicked into things.",
    "Why did the document get promoted? It had outstanding margins.",
    "Why did the laptop bring a sweater? It kept getting cold starts.",
    "Why was the notebook so optimistic? It always had another page to turn.",
)


def canned_safe_joke(*, request_text: str) -> SafeJokeResponse:
    """Return a deterministic SFW fallback joke for guarded or failed runs."""

    lowered = request_text.lower()
    for keywords, joke_text in _SAFE_FALLBACK_JOKES:
        if any(keyword in lowered for keyword in keywords):
            return SafeJokeResponse(
                request_summary=_fallback_request_summary(request_text),
                joke_text=joke_text,
                safe_for_work=True,
                content_rating="g",
            )

    digest = hashlib.sha256(lowered.encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(_GENERIC_FALLBACK_JOKES)
    return SafeJokeResponse(
        request_summary=_fallback_request_summary(request_text),
        joke_text=_GENERIC_FALLBACK_JOKES[index],
        safe_for_work=True,
        content_rating="g",
    )


def _fallback_request_summary(request_text: str) -> str:
    cleaned = " ".join(request_text.strip().split())
    if not cleaned:
        return "general safe-for-work joke request"
    if len(cleaned) <= 80:
        return cleaned
    return f"{cleaned[:77].rstrip()}..."
