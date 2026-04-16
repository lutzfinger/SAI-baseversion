"""Shared LangChain-backed structured-output LLM client."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel

from app.observability.langsmith import (
    create_langsmith_client,
    langsmith_trace_details,
    langsmith_tracing_context,
)
from app.shared.config import Settings


@dataclass
class StructuredLLMResponse:
    payload: dict[str, Any]
    raw_text: str
    usage: dict[str, Any] | None = None
    response_details: dict[str, Any] | None = None


def llm_exception_details(exc: Exception) -> dict[str, Any]:
    return {
        "error_type": exc.__class__.__name__,
        "error_message": str(exc),
    }


class LangChainStructuredClient:
    """Small structured-output client shared by starter LLM tools."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        settings: Settings,
        timeout_seconds: int,
        max_output_tokens: int | None = None,
        run_name: str,
        run_tags: list[str] | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.provider = provider.strip().lower()
        self.model = model
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.run_name = run_name
        self.run_tags = list(run_tags or [])
        self.run_metadata = dict(run_metadata or {})
        self.langsmith_client = create_langsmith_client(settings)
        self.chat_model = self._build_chat_model()

    def classify(
        self,
        *,
        prompt: str,
        model: str,
        response_schema: dict[str, Any],
        response_model: type[BaseModel] | None = None,
        max_output_tokens: int | None = None,
    ) -> StructuredLLMResponse:
        del max_output_tokens
        schema = response_model if response_model is not None else response_schema
        runnable = self._build_structured_runnable(schema=schema)
        config: dict[str, Any] = {
            "run_name": self.run_name,
            "tags": list(self.run_tags),
            "metadata": {
                **self.run_metadata,
                "provider": self.provider,
                "model": model,
            },
        }
        with langsmith_tracing_context(self.settings, client=self.langsmith_client):
            result = runnable.invoke(prompt, config=config)
        parsed_payload, raw_message = _extract_structured_result(result)
        if response_model is not None:
            payload = response_model.model_validate(parsed_payload).model_dump(mode="json")
        else:
            payload = parsed_payload
        response_id = getattr(raw_message, "id", None) if raw_message is not None else None
        return StructuredLLMResponse(
            payload=payload,
            raw_text=_extract_raw_text(raw_message=raw_message, parsed_payload=payload),
            usage=_extract_usage(raw_message),
            response_details={
                "provider": self.provider,
                "model": model,
                "response_id": response_id,
                "response_metadata": (
                    _json_safe(getattr(raw_message, "response_metadata", None))
                    if raw_message is not None
                    else None
                ),
                **langsmith_trace_details(self.settings),
            },
        )

    def _build_structured_runnable(self, *, schema: Any) -> Any:
        if self.provider == "openai":
            return self.chat_model.with_structured_output(
                schema,
                method="json_schema",
                include_raw=True,
            )
        return self.chat_model.with_structured_output(schema, include_raw=True)

    def _build_chat_model(self) -> Any:
        if self.provider == "openai":
            return _build_openai_chat_model(
                settings=self.settings,
                model=self.model,
                timeout_seconds=self.timeout_seconds,
                max_output_tokens=self.max_output_tokens,
            )
        if self.provider == "ollama":
            return _build_ollama_chat_model(
                settings=self.settings,
                model=self.model,
                timeout_seconds=self.timeout_seconds,
                max_output_tokens=self.max_output_tokens,
            )
        raise RuntimeError(f"Unsupported LangChain LLM provider: {self.provider}")


def _build_openai_chat_model(
    *,
    settings: Settings,
    model: str,
    timeout_seconds: int,
    max_output_tokens: int | None,
) -> Any:
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("The langchain-openai package is not installed.") from exc

    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required for cloud LangChain model calls.")
    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": settings.openai_api_key,
        "timeout": timeout_seconds,
        "temperature": 0,
        "metadata": {"ls_provider": "openai", "ls_model_name": model},
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    if max_output_tokens is not None:
        kwargs["max_tokens"] = max_output_tokens
    return ChatOpenAI(**kwargs)


def _build_ollama_chat_model(
    *,
    settings: Settings,
    model: str,
    timeout_seconds: int,
    max_output_tokens: int | None,
) -> Any:
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("The langchain-ollama package is not installed.") from exc

    kwargs: dict[str, Any] = {
        "model": model,
        "base_url": settings.local_llm_host,
        "temperature": 0,
        "client_kwargs": {"timeout": timeout_seconds},
        "metadata": {"ls_provider": "ollama", "ls_model_name": model},
    }
    if max_output_tokens is not None:
        kwargs["num_predict"] = max_output_tokens
    return ChatOllama(**kwargs)


def _extract_structured_result(result: Any) -> tuple[dict[str, Any], Any | None]:
    parsed = result
    raw_message = None
    if isinstance(result, dict) and "parsed" in result:
        parsing_error = result.get("parsing_error")
        if parsing_error is not None:
            raise parsing_error
        parsed = result.get("parsed")
        raw_message = result.get("raw")
    if parsed is None:
        raise RuntimeError("Structured LLM returned no parsed payload.")
    if isinstance(parsed, BaseModel):
        return parsed.model_dump(mode="json"), raw_message
    return _coerce_payload_to_dict(parsed), raw_message


def _extract_raw_text(*, raw_message: Any | None, parsed_payload: dict[str, Any]) -> str:
    if raw_message is not None:
        content = getattr(raw_message, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text_chunks: list[str] = []
            for block in content:
                if isinstance(block, str) and block.strip():
                    text_chunks.append(block.strip())
                    continue
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        text_chunks.append(text.strip())
            if text_chunks:
                return "\n".join(text_chunks)
    return json.dumps(parsed_payload, sort_keys=True)


def _extract_usage(raw_message: Any | None) -> dict[str, Any] | None:
    if raw_message is None:
        return None
    usage_metadata = getattr(raw_message, "usage_metadata", None)
    if isinstance(usage_metadata, dict):
        return cast(dict[str, Any], _json_safe(usage_metadata))
    response_metadata = getattr(raw_message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        token_usage = response_metadata.get("token_usage")
        if isinstance(token_usage, dict):
            return cast(dict[str, Any], _json_safe(token_usage))
    return None


def _coerce_payload_to_dict(parsed: Any) -> dict[str, Any]:
    if isinstance(parsed, dict):
        return cast(dict[str, Any], _json_safe(parsed))
    return {"value": _json_safe(parsed)}


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
