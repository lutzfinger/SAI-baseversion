"""Graph-backed worker for the email triage workflow.

This worker is intentionally thin. It prepares the serializable runtime context
for one workflow run, then hands execution to the LangGraph runtime in
`app/graphs/email_triage.py`. That split keeps workflow logic scalable without
moving policy or audit concerns out of the control plane.
"""

from __future__ import annotations

from app.control_plane.graph_runtime import GraphRuntimeManager
from app.graphs.email_triage import EmailTriageRuntimeContext, EmailTriageStateGraphRuntime
from app.learning.local_cloud_comparison import (
    ModelRuntimeReference,
    record_local_cloud_comparisons_best_effort,
)
from app.observability.langsmith import LangSmithTraceManager
from app.shared.config import Settings
from app.shared.models import PolicyDocument, PromptDocument, WorkflowToolDefinition
from app.workers.email_models import EmailMessage, EmailTriageResult


class EmailTriageWorker:
    """Small scoped worker that delegates step execution to LangGraph."""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime_manager: GraphRuntimeManager,
        langsmith: LangSmithTraceManager,
    ) -> None:
        self.settings = settings
        self.runtime_manager = runtime_manager
        self.graph_runtime = EmailTriageStateGraphRuntime(
            settings=settings,
            runtime_manager=runtime_manager,
            langsmith=langsmith,
        )

    def classify_messages(
        self,
        *,
        run_id: str,
        workflow_id: str,
        messages: list[EmailMessage],
        prompts_by_tool_id: dict[str, PromptDocument],
        tool_definitions: list[WorkflowToolDefinition],
        policy: PolicyDocument,
        operator_email: str,
        local_llm_available: bool = True,
        local_llm_reason: str | None = None,
        local_llm_provider: str | None = None,
        local_llm_model: str | None = None,
        local_llm_host: str | None = None,
        cloud_llm_available: bool = True,
        cloud_llm_reason: str | None = None,
        cloud_llm_provider: str | None = None,
        cloud_llm_model: str | None = None,
        cloud_llm_host: str | None = None,
    ) -> list[EmailTriageResult]:
        """Classify a batch of messages through the graph-backed runtime."""

        runtime_context = EmailTriageRuntimeContext(
            operator_email=operator_email,
            policy=policy,
            prompts_by_tool_id=prompts_by_tool_id,
            tool_definitions=tool_definitions,
            local_llm_available=local_llm_available,
            local_llm_reason=local_llm_reason,
            local_llm_provider=local_llm_provider,
            local_llm_model=local_llm_model,
            local_llm_host=local_llm_host,
            cloud_llm_available=cloud_llm_available,
            cloud_llm_reason=cloud_llm_reason,
            cloud_llm_provider=cloud_llm_provider,
            cloud_llm_model=cloud_llm_model,
            cloud_llm_host=cloud_llm_host,
        )
        results = self.graph_runtime.classify_messages(
            run_id=run_id,
            workflow_id=workflow_id,
            messages=messages,
            runtime_context=runtime_context,
        )
        record_local_cloud_comparisons_best_effort(
            settings=self.settings,
            run_id=run_id,
            workflow_id=workflow_id,
            messages=messages,
            results=results,
            prompts_by_tool_id=prompts_by_tool_id,
            tool_definitions=tool_definitions,
            local_runtime=ModelRuntimeReference(
                provider=local_llm_provider,
                model=local_llm_model,
                host=local_llm_host,
            ),
            cloud_runtime=ModelRuntimeReference(
                provider=cloud_llm_provider,
                model=cloud_llm_model,
                host=cloud_llm_host,
            ),
        )
        return results
