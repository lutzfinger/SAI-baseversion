"""Worker for the interactive propose-first task assistant."""

from __future__ import annotations

from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.models import ToolExecutionRecord
from app.tools.task_assistant import TaskApproachPlannerTool
from app.workers.task_assistant_models import TaskExecutionPlan


class TaskAssistantWorker:
    """Create structured execution plans for free-form operator task requests."""

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings

    def plan_request(
        self,
        *,
        task_text: str,
        context_lines: list[str],
        requested_by: str,
        prompt: PromptDocument,
        tool_definition: WorkflowToolDefinition,
    ) -> tuple[TaskExecutionPlan, ToolExecutionRecord]:
        planner = TaskApproachPlannerTool(
            tool_definition=tool_definition,
            prompt=prompt,
            settings=self.settings,
        )
        return planner.plan(
            task_text=task_text,
            context_lines=context_lines,
            requested_by=requested_by,
        )
