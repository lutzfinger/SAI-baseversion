"""Helpers for optional LangSmith tracing in starter workflows."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.shared.config import Settings


def langsmith_trace_details(settings: Settings) -> dict[str, Any]:
    """Return small structured tracing metadata for tool records."""

    enabled = settings.langsmith_tracing_enabled()
    return {
        "langsmith_tracing": enabled,
        "langsmith_project": settings.langsmith_project if enabled else None,
    }


def create_langsmith_client(settings: Settings) -> Any | None:
    """Build a LangSmith client when tracing is enabled."""

    if not settings.langsmith_tracing_enabled():
        return None
    try:
        from langsmith import Client
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("The langsmith package is not installed.") from exc

    client_kwargs: dict[str, Any] = {"api_key": settings.langsmith_api_key}
    if settings.langsmith_endpoint:
        client_kwargs["api_url"] = settings.langsmith_endpoint
    if settings.langsmith_workspace_id:
        client_kwargs["workspace_id"] = settings.langsmith_workspace_id
    return Client(**client_kwargs)


@contextmanager
def langsmith_tracing_context(
    settings: Settings,
    *,
    client: Any | None = None,
) -> Iterator[None]:
    """Enable LangSmith tracing for one block when configured."""

    if not settings.langsmith_tracing_enabled():
        yield
        return
    try:
        from langsmith.run_helpers import tracing_context
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("The langsmith package is not installed.") from exc

    with tracing_context(
        enabled=True,
        project_name=settings.langsmith_project,
        client=client or create_langsmith_client(settings),
    ):
        yield


def flush_langsmith_tracers() -> None:
    """Block until outstanding LangChain/LangSmith tracer callbacks complete."""

    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers
    except ImportError:  # pragma: no cover
        return
    wait_for_all_tracers()


_OPENAI_CLIENT_WRAPPED_ATTR = "_sai_langsmith_wrapped"


def instrument_openai_client[OpenAIClientT](
    client: OpenAIClientT,
    *,
    settings: Settings,
    run_name: str,
    metadata: dict[str, Any] | None = None,
) -> OpenAIClientT:
    """Wrap an OpenAI client so its SDK calls appear inside the active LangSmith trace.

    No-op when LangSmith tracing is disabled or the langsmith package is missing.
    The original client is returned unchanged in either case.
    """

    if not settings.langsmith_tracing_enabled():
        return client
    if getattr(client, _OPENAI_CLIENT_WRAPPED_ATTR, False):
        return client
    try:
        from langsmith.wrappers import wrap_openai
    except ImportError:  # pragma: no cover - langsmith optional in starter
        return client

    tracing_extra: dict[str, Any] | None = None
    if metadata:
        tracing_extra = {"metadata": metadata}
    wrapped = wrap_openai(
        client,
        tracing_extra=tracing_extra,  # type: ignore[arg-type]
        chat_name=run_name,
        completions_name=run_name,
    )
    try:
        setattr(wrapped, _OPENAI_CLIENT_WRAPPED_ATTR, True)
    except Exception:  # pragma: no cover - defensive
        pass
    return wrapped
