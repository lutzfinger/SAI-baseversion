"""Small starter control plane for the SAI baseversion repo."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.approvals.service import ApprovalService
from app.connectors.gmail import GmailAPIConnector
from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.connectors.gmail_labels import GmailLabelConnector
from app.connectors.slack import SlackPostConnector
from app.control_plane.loaders import PolicyStore, PromptStore, WorkflowStore
from app.learning.fact_memory import FactMemoryStore, extract_operator_facts, render_fact_context
from app.observability.audit import AuditLogger
from app.observability.langsmith import flush_langsmith_tracers
from app.observability.run_store import RunStore
from app.observability.task_plane_models import TaskEventRecord, TaskRecord, TaskStepRecord, utc_now
from app.shared.config import Settings
from app.shared.models import RunRecord, RunStatus, WorkflowDefinition
from app.shared.run_ids import new_id, new_run_id
from app.shared.tool_registry import validate_workflow_policy_against_specs
from app.tools.task_assistant import build_safe_workflow_catalog
from app.workers.newsletter_identifier import NewsletterIdentifierWorker
from app.workers.newsletter_identifier_models import (
    NewsletterIdentifierResult,
    build_newsletter_artifact,
)
from app.workers.sai_email_interaction import (
    SaiEmailInteractionWorker,
    looks_like_email_approval,
)
from app.workers.sai_email_interaction_models import (
    SaiEmailGenericPlan,
    SaiEmailInteractionItem,
    SaiEmailInteractionResult,
    SaiEmailThreadState,
    build_sai_email_interaction_artifact,
)
from app.workers.task_assistant_models import (
    TaskExecutionOutcome,
    TaskExecutionPlan,
    TaskExecutionStepResult,
)

OPEN_TASK_STATUSES = {"awaiting_information", "awaiting_approval", "in_progress"}


class WorkflowExecutionResult(BaseModel):
    """Compact response returned after one workflow run."""

    run_id: str
    workflow_id: str
    status: str
    summary: dict[str, Any] = Field(default_factory=dict)
    artifact_path: str | None = None


class ControlPlane:
    """Starter control plane with just email, slack, newsletter, and email-native planning."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_runtime_paths()
        self.prompt_store = PromptStore(settings.prompts_dir)
        self.policy_store = PolicyStore(settings.policies_dir)
        self.workflow_store = WorkflowStore(settings.workflows_dir)
        self.run_store = RunStore(settings.database_path)
        self.audit_logger = AuditLogger(settings)
        self.approval_service = ApprovalService(self.run_store, self.audit_logger)
        self.fact_memory = FactMemoryStore(settings.fact_memory_database_path)
        self.newsletter_worker = NewsletterIdentifierWorker(settings=settings)
        self.sai_email_worker = SaiEmailInteractionWorker(settings=settings)

    def close(self) -> None:
        flush_langsmith_tracers()
        return None

    def list_workflows(self) -> list[dict[str, Any]]:
        workflows = []
        for workflow in self.workflow_store.list_workflows():
            workflows.append(
                {
                    "workflow_id": workflow.workflow_id,
                    "description": workflow.description,
                    "connector": workflow.connector,
                    "tags": list(workflow.tags),
                    "tools": [tool.kind for tool in workflow.tools if tool.enabled],
                }
            )
        return workflows

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in self.run_store.list_runs(limit=limit)]

    def get_status(self) -> dict[str, Any]:
        workflows = self.workflow_store.list_workflows()
        runs = self.run_store.list_runs(limit=10)
        tasks = self.run_store.list_tasks(limit=20)
        return {
            "app_name": self.settings.app_name,
            "environment": self.settings.environment,
            "workflow_count": len(workflows),
            "recent_run_count": len(runs),
            "open_task_count": sum(1 for task in tasks if task.status in OPEN_TASK_STATUSES),
            "sai_alias_email": self.settings.sai_alias_email,
        }

    def get_run_detail(self, run_id: str) -> dict[str, Any]:
        run = self.run_store.get_run(run_id)
        artifact_path = self.settings.artifacts_dir / run_id / "result.json"
        artifact: dict[str, Any] | None = None
        if artifact_path.exists():
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        return {
            "run": run.model_dump(mode="json"),
            "events": [
                event.model_dump(mode="json")
                for event in self.audit_logger.read_events(run_id=run_id)
            ],
            "artifact": artifact,
            "artifact_path": str(artifact_path) if artifact_path.exists() else None,
        }

    def get_run_events(self, run_id: str) -> list[dict[str, Any]]:
        events = self.audit_logger.read_events(run_id=run_id)
        return [event.model_dump(mode="json") for event in events]

    def authenticate_gmail(self, workflow_id: str) -> dict[str, Any]:
        workflow = self.workflow_store.load(f"{workflow_id}.yaml")
        policy = self.policy_store.load(workflow.policy)
        token_path = GmailOAuthAuthenticator(
            settings=self.settings, policy=policy
        ).authenticate_interactively()
        return {"workflow_id": workflow_id, "token_path": str(token_path)}

    def run_workflow(
        self,
        *,
        workflow_id: str,
        source_override: str | None = None,
        connector_overrides: dict[str, Any] | None = None,
    ) -> WorkflowExecutionResult:
        del source_override
        workflow = self.workflow_store.load(f"{workflow_id}.yaml")
        policy = self.policy_store.load(workflow.policy)
        validate_workflow_policy_against_specs(workflow=workflow, policy=policy)
        run_id = new_run_id(workflow.workflow_id)
        started_at = utc_now()
        self.run_store.create_run(
            RunRecord(
                run_id=run_id,
                workflow_id=workflow.workflow_id,
                status=RunStatus.PENDING,
                started_at=started_at,
                updated_at=started_at,
                summary={},
            )
        )
        self.audit_logger.append_event(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            actor="control-plane",
            component="runner",
            event_type="run.started",
            payload={"connector_overrides": connector_overrides or {}},
        )
        self.run_store.update_run_status(run_id, RunStatus.RUNNING)
        result_model: NewsletterIdentifierResult | SaiEmailInteractionResult
        try:
            if workflow.worker == "newsletter_identifier":
                result_model, artifact_path = self._run_newsletter_workflow(
                    run_id=run_id,
                    workflow=workflow,
                    policy=policy,
                    connector_overrides=connector_overrides or {},
                )
                summary = result_model.model_dump(mode="json")
            elif workflow.worker == "starter_email_interaction":
                result_model, artifact_path = self._run_sai_email_workflow(
                    run_id=run_id,
                    workflow=workflow,
                    policy=policy,
                    connector_overrides=connector_overrides or {},
                )
                summary = result_model.model_dump(mode="json")
            else:
                raise ValueError(f"Unsupported starter worker: {workflow.worker}")
            self.run_store.update_run_status(run_id, RunStatus.COMPLETED, summary=summary)
            self.audit_logger.append_event(
                run_id=run_id,
                workflow_id=workflow.workflow_id,
                actor="control-plane",
                component="runner",
                event_type="run.completed",
                payload={"artifact_path": artifact_path, "summary": summary},
            )
            return WorkflowExecutionResult(
                run_id=run_id,
                workflow_id=workflow.workflow_id,
                status=RunStatus.COMPLETED.value,
                summary=summary,
                artifact_path=artifact_path,
            )
        except Exception as exc:
            summary = {"error": str(exc)}
            self.run_store.update_run_status(run_id, RunStatus.FAILED, summary=summary)
            self.audit_logger.append_event(
                run_id=run_id,
                workflow_id=workflow.workflow_id,
                actor="control-plane",
                component="runner",
                event_type="run.failed",
                payload=summary,
            )
            raise

    def _run_newsletter_workflow(
        self,
        *,
        run_id: str,
        workflow: WorkflowDefinition,
        policy: Any,
        connector_overrides: dict[str, Any],
    ) -> tuple[NewsletterIdentifierResult, str]:
        prompts_by_tool_id = self._load_tool_prompts(workflow)
        authenticator = GmailOAuthAuthenticator(settings=self.settings, policy=policy)
        query = (
            str(
                connector_overrides.get("query") or workflow.connector_config.get("query") or ""
            ).strip()
            or None
        )
        label_ids = connector_overrides.get(
            "label_ids",
            workflow.connector_config.get("label_ids", ["INBOX"]),
        )
        max_results = int(
            connector_overrides.get(
                "max_results",
                workflow.connector_config.get("max_results", 100),
            )
        )
        connector = GmailAPIConnector(
            authenticator=authenticator,
            query=query,
            label_ids=label_ids,
            max_results=max_results,
            include_spam_trash=bool(
                connector_overrides.get(
                    "include_spam_trash",
                    workflow.connector_config.get("include_spam_trash", False),
                )
            ),
        )
        self._enforce_connector_actions(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            policy=policy,
            actor="workflow",
            actions=connector.required_actions(),
        )
        messages = self._latest_message_per_thread(connector.fetch_messages())
        pending_messages = self._filter_unprocessed_messages(workflow.workflow_id, messages)
        label_connector = None
        tagging_enabled = workflow.workflow_id.endswith("-tagging")
        if tagging_enabled:
            label_connector = GmailLabelConnector(authenticator=authenticator)
            self._enforce_connector_actions(
                run_id=run_id,
                workflow_id=workflow.workflow_id,
                policy=policy,
                actor="workflow",
                actions=label_connector.required_actions(),
            )
        result = self.newsletter_worker.classify_messages(
            workflow_id=workflow.workflow_id,
            run_id=run_id,
            messages=pending_messages,
            prompts_by_tool_id=prompts_by_tool_id,
            tool_definitions=workflow.tools,
            operator_email=self.settings.user_email,
            label_connector=label_connector,
            tagging_enabled=tagging_enabled,
        )
        metadata_by_item = {
            item.message.thread_id or item.message.message_id: {
                "last_processed_message_id": item.message.message_id,
                "classification": item.classification.model_dump(mode="json"),
            }
            for item in result.items
        }
        self.run_store.mark_processed_items(
            workflow_id=workflow.workflow_id,
            run_id=run_id,
            item_ids=[item.message.thread_id or item.message.message_id for item in result.items],
            metadata_by_item=metadata_by_item,
        )
        artifact = build_newsletter_artifact(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            prompts=list(prompts_by_tool_id.values()),
            runtime={"connector": workflow.connector},
            items=result.items,
        )
        artifact_path = self._write_artifact(
            run_id=run_id, payload=artifact.model_dump(mode="json")
        )
        return result, artifact_path

    def _run_sai_email_workflow(
        self,
        *,
        run_id: str,
        workflow: WorkflowDefinition,
        policy: Any,
        connector_overrides: dict[str, Any],
    ) -> tuple[SaiEmailInteractionResult, str]:
        prompts_by_tool_id = self._load_tool_prompts(workflow)
        authenticator = GmailOAuthAuthenticator(settings=self.settings, policy=policy)
        label_ids = connector_overrides.get(
            "label_ids",
            workflow.connector_config.get("label_ids", []),
        )
        max_results = int(
            connector_overrides.get(
                "max_results",
                workflow.connector_config.get("max_results", 25),
            )
        )
        inbox_connector = GmailAPIConnector(
            authenticator=authenticator,
            query=str(
                connector_overrides.get("query")
                or workflow.connector_config.get("query")
                or f"to:{self.settings.sai_alias_email}"
            ),
            label_ids=label_ids,
            max_results=max_results,
        )
        self._enforce_connector_actions(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            policy=policy,
            actor="workflow",
            actions=inbox_connector.required_actions(),
        )
        document_connector = self.sai_email_worker.build_document_connector(policy=policy)
        send_connector = self.sai_email_worker.build_send_connector(policy=policy)
        self._enforce_connector_actions(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            policy=policy,
            actor="workflow",
            actions=document_connector.required_actions(),
        )
        self._enforce_connector_actions(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            policy=policy,
            actor="workflow",
            actions=send_connector.required_actions(),
        )
        messages = self._latest_message_per_thread(inbox_connector.fetch_messages())
        messages.extend(self._refresh_open_thread_messages(workflow.workflow_id, inbox_connector))
        candidates = self._latest_message_per_thread(messages)
        items: list[SaiEmailInteractionItem] = []
        reviewed_thread_ids: list[str] = []
        workflow_catalog = build_safe_workflow_catalog(self.workflow_store.list_workflows())
        for message in candidates:
            thread_id = message.thread_id or message.message_id
            reviewed_thread_ids.append(thread_id)
            if not self.sai_email_worker.is_allowed_request_sender(
                policy=policy,
                from_email=message.from_email,
            ):
                continue
            existing_task = self._find_open_task_for_thread(workflow.workflow_id, thread_id)
            document = document_connector.fetch_document(message_id=message.message_id)
            request_text = document.combined_text()
            facts = extract_operator_facts(
                text=request_text,
                source_workflow_id=workflow.workflow_id,
                source_run_id=run_id,
                source_reference=message.subject,
                source_thread_id=thread_id,
                source_message_id=message.message_id,
            )
            self.fact_memory.record_facts(facts)
            known_facts = render_fact_context(
                self.fact_memory.query_relevant_facts(
                    query_text=request_text,
                    workflow_id=workflow.workflow_id,
                )
            )
            if existing_task is not None and looks_like_email_approval(request_text):
                item = self._handle_email_approval(
                    run_id=run_id,
                    workflow=workflow,
                    policy=policy,
                    message=message,
                    task=existing_task,
                )
                items.append(item)
                continue

            thread_messages = inbox_connector.fetch_thread_messages(thread_id=thread_id)
            thread_state_summary: dict[str, object] = {
                "message_count": len(thread_messages),
                "last_subject": message.subject,
                "last_sender": message.from_email,
                "open_task_status": existing_task.status if existing_task is not None else None,
            }
            task_context_summary: dict[str, object] = {
                "existing_task_id": existing_task.task_id if existing_task is not None else None,
                "existing_status": existing_task.status if existing_task is not None else None,
                "pending_question": (
                    existing_task.pending_question if existing_task is not None else None
                ),
            }
            tool_definition = next(
                tool for tool in workflow.tools if tool.kind == "sai_email_planner"
            )
            plan, planner_record = self.sai_email_worker.plan_generic_request(
                request_message_id=message.message_id,
                thread_id=thread_id,
                request_text=request_text,
                thread_state_summary=thread_state_summary,
                task_context_summary=task_context_summary,
                known_facts=known_facts,
                read_only_context={
                    "current_time": datetime.now(UTC).isoformat(),
                    "thread_preview": [
                        {
                            "message_id": thread_message.message_id,
                            "from_email": thread_message.from_email,
                            "subject": thread_message.subject,
                            "snippet": thread_message.snippet,
                        }
                        for thread_message in thread_messages[-5:]
                    ],
                },
                workflow_catalog=workflow_catalog,
                prompt=prompts_by_tool_id[tool_definition.tool_id],
                tool_definition=tool_definition,
            )
            task = self._upsert_email_task(
                run_id=run_id,
                workflow=workflow,
                message=message,
                existing_task=existing_task,
                plan=plan,
            )
            response_message = self._send_task_reply(
                run_id=run_id,
                workflow_id=workflow.workflow_id,
                policy=policy,
                message=message,
                body=self.sai_email_worker.format_reply(
                    short_response=plan.short_response,
                    explanation=plan.explanation,
                ),
            )
            task_state = SaiEmailThreadState(
                thread_id=thread_id,
                task_id=task.task_id,
                request_kind=plan.request_kind,
                status=task.status,  # type: ignore[arg-type]
                request_message_id=message.message_id,
                request_subject=message.subject,
                approval_request_id=(
                    task.approval_request_ids[0] if task.approval_request_ids else None
                ),
                current_plan=task.current_plan,
                pending_question=task.pending_question,
                short_response=plan.short_response,
                explanation=plan.explanation,
                last_processed_message_id=message.message_id,
                last_response_message_id=response_message.get("message_id"),
                reply_recipient_email=response_message.get("to_email"),
                activity_ids=[activity.activity_id for activity in plan.activities],
            )
            self._record_task_thread_state(task=task, thread_state=task_state)
            self.sai_email_worker.persist_activities(
                workflow_id=workflow.workflow_id,
                run_id=run_id,
                thread_id=thread_id,
                message_id=message.message_id,
                activities=[activity.model_dump(mode="json") for activity in plan.activities],
            )
            items.append(
                SaiEmailInteractionItem(
                    request_message=message,
                    request_kind=plan.request_kind,
                    response_mode=plan.response_mode,
                    short_response=plan.short_response,
                    explanation=plan.explanation,
                    activities=plan.activities,
                    approval_request_id=(
                        task.approval_request_ids[0] if task.approval_request_ids else None
                    ),
                    response_message_id=response_message.get("message_id"),
                    thread_state=task_state,
                    tool_records=[planner_record],
                )
            )

        result = SaiEmailInteractionResult(
            reviewed_thread_count=len(reviewed_thread_ids),
            replied_count=len(items),
            awaiting_information_count=sum(
                1 for item in items if item.response_mode == "ask_information"
            ),
            awaiting_approval_count=sum(
                1 for item in items if item.response_mode == "ask_approval"
            ),
            completed_count=sum(
                1 for item in items if item.response_mode in {"completed", "answer_only"}
            ),
            failed_count=sum(1 for item in items if item.response_mode == "failed"),
            items=items,
        )
        metadata_by_item = {
            item.request_message.thread_id or item.request_message.message_id: {
                "last_processed_message_id": item.request_message.message_id,
                "response_mode": item.response_mode,
                "request_kind": item.request_kind,
            }
            for item in items
        }
        self.run_store.mark_processed_items(
            workflow_id=workflow.workflow_id,
            run_id=run_id,
            item_ids=list(metadata_by_item),
            metadata_by_item=metadata_by_item,
        )
        artifact = build_sai_email_interaction_artifact(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            prompts=list(prompts_by_tool_id.values()),
            runtime={"connector": workflow.connector},
            reviewed_thread_ids=reviewed_thread_ids,
            result=result,
        )
        artifact_path = self._write_artifact(
            run_id=run_id, payload=artifact.model_dump(mode="json")
        )
        return result, artifact_path

    def _handle_email_approval(
        self,
        *,
        run_id: str,
        workflow: WorkflowDefinition,
        policy: Any,
        message: Any,
        task: TaskRecord,
    ) -> SaiEmailInteractionItem:
        approval_request_id = task.approval_request_ids[0] if task.approval_request_ids else None
        if approval_request_id is None:
            raise ValueError("Approval email received for a task without approval_request_id.")
        self.approval_service.decide(
            request_id=approval_request_id,
            approved=True,
            decided_by=message.from_email,
            reason="email approval",
        )
        execution_plan = TaskExecutionPlan.model_validate(task.current_plan)
        execution = self._execute_task_plan(
            run_id=run_id,
            workflow=workflow,
            policy=policy,
            execution_plan=execution_plan,
            approved_by=message.from_email,
        )
        short_response = "Done."
        explanation = (
            f"SAI executed {execution.completed_action_count} approved action(s) "
            f"and recorded the outcome in the task plane."
        )
        response_message = self._send_task_reply(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            policy=policy,
            message=message,
            body=self.sai_email_worker.format_reply(
                short_response=short_response,
                explanation=explanation,
            ),
        )
        updated_task = task.model_copy(
            update={
                "status": "completed",
                "pending_question": None,
                "updated_at": utc_now(),
                "completed_at": utc_now(),
                "last_run_id": run_id,
            }
        )
        self.run_store.upsert_task(updated_task)
        self.run_store.append_task_events(
            [
                TaskEventRecord(
                    event_id=new_id("taskevt"),
                    task_id=task.task_id,
                    workflow_id=workflow.workflow_id,
                    run_id=run_id,
                    event_kind="approval_resolved",
                    summary="Approval resolved by email and plan executed.",
                    status="approved",
                    payload={"approval_request_id": approval_request_id},
                    created_at=utc_now(),
                )
            ]
        )
        self.sai_email_worker.persist_golden_record(
            golden_id=new_id("golden"),
            thread_id=message.thread_id or message.message_id,
            request_message_id=task.source_message_id or message.message_id,
            workflow_id=workflow.workflow_id,
            run_id=run_id,
            approved_by=message.from_email,
            request_kind=task.task_kind or "workflow_suggestion",
            response_mode="completed",
            short_response=short_response,
            explanation=explanation,
            activity_ids=[],
            approval_request_id=approval_request_id,
            execution_status="completed",
            metadata={"task_id": task.task_id},
        )
        thread_state = SaiEmailThreadState(
            thread_id=message.thread_id or message.message_id,
            task_id=task.task_id,
            request_kind=task.task_kind or "workflow_suggestion",
            status="completed",
            request_message_id=task.source_message_id or message.message_id,
            request_subject=message.subject,
            approval_request_id=approval_request_id,
            current_plan=task.current_plan,
            short_response=short_response,
            explanation=explanation,
            last_processed_message_id=message.message_id,
            last_response_message_id=response_message.get("message_id"),
            reply_recipient_email=response_message.get("to_email"),
        )
        self._record_task_thread_state(task=updated_task, thread_state=thread_state)
        return SaiEmailInteractionItem(
            request_message=message,
            request_kind=task.task_kind or "workflow_suggestion",
            response_mode="completed",
            short_response=short_response,
            explanation=explanation,
            approval_request_id=approval_request_id,
            response_message_id=response_message.get("message_id"),
            thread_state=thread_state,
        )

    def _execute_task_plan(
        self,
        *,
        run_id: str,
        workflow: WorkflowDefinition,
        policy: Any,
        execution_plan: TaskExecutionPlan,
        approved_by: str,
    ) -> TaskExecutionOutcome:
        step_results: list[TaskExecutionStepResult] = []
        for action in execution_plan.actions:
            if action.action_kind == "run_workflow":
                nested = self.run_workflow(
                    workflow_id=str(action.workflow_id),
                    connector_overrides=dict(action.connector_overrides),
                )
                step_results.append(
                    TaskExecutionStepResult(
                        action_id=action.action_id,
                        action_kind=action.action_kind,
                        status="completed",
                        result={"nested_run_id": nested.run_id, "workflow_id": nested.workflow_id},
                    )
                )
            elif action.action_kind == "post_slack_message":
                self._post_slack_message(
                    policy=policy, channel=action.channel or "", text=action.text or ""
                )
                step_results.append(
                    TaskExecutionStepResult(
                        action_id=action.action_id,
                        action_kind=action.action_kind,
                        status="completed",
                        result={"channel": action.channel},
                    )
                )
        return TaskExecutionOutcome(
            approved=True,
            approved_by=approved_by,
            approved_at=utc_now(),
            step_results=step_results,
            completed_action_count=sum(1 for step in step_results if step.status == "completed"),
            failed_action_count=sum(1 for step in step_results if step.status == "failed"),
        )

    def _post_slack_message(self, *, policy: Any, channel: str, text: str) -> dict[str, Any]:
        connector = SlackPostConnector(policy=policy, default_channel=channel)
        return connector.post_message(text=text, channel=channel)

    def _upsert_email_task(
        self,
        *,
        run_id: str,
        workflow: WorkflowDefinition,
        message: Any,
        existing_task: TaskRecord | None,
        plan: SaiEmailGenericPlan,
    ) -> TaskRecord:
        now = utc_now()
        approval_request_ids: list[str] = []
        status: str
        pending_question: str | None = None
        current_plan: dict[str, Any] = {}
        if plan.response_mode == "ask_approval" and plan.execution_plan is not None:
            approval = self.approval_service.request_approval(
                run_id=run_id,
                workflow_id=workflow.workflow_id,
                action="task.execute_plan",
                reason=plan.execution_plan.operator_approval_question,
                requested_by="sai-email-interaction",
                metadata={"message_id": message.message_id},
            )
            approval_request_ids = [approval.request_id]
            status = "awaiting_approval"
            current_plan = plan.execution_plan.model_dump(mode="json")
        elif plan.response_mode == "ask_information":
            status = "awaiting_information"
            pending_question = plan.follow_up_question
        elif plan.response_mode in {"completed", "answer_only"}:
            status = "completed"
        else:
            status = "failed"
        task = TaskRecord(
            task_id=existing_task.task_id if existing_task is not None else new_id("task"),
            workflow_id=workflow.workflow_id,
            source_kind="gmail_thread",
            source_thread_id=message.thread_id or message.message_id,
            source_message_id=message.message_id,
            requested_by=message.from_email,
            title=message.subject,
            task_kind=plan.request_kind,
            status=status,  # type: ignore[arg-type]
            current_plan=current_plan,
            pending_question=pending_question,
            approval_request_ids=approval_request_ids,
            linked_thread_ids=[message.thread_id] if message.thread_id else [],
            linked_message_ids=[message.message_id],
            opaque_payload=existing_task.opaque_payload if existing_task is not None else {},
            last_run_id=run_id,
            failure_reason=plan.explanation if status == "failed" else None,
            created_at=existing_task.created_at if existing_task is not None else now,
            updated_at=now,
            completed_at=now if status == "completed" else None,
        )
        self.run_store.upsert_task(task)
        self.run_store.append_task_events(
            [
                TaskEventRecord(
                    event_id=new_id("taskevt"),
                    task_id=task.task_id,
                    workflow_id=workflow.workflow_id,
                    run_id=run_id,
                    event_kind="task_created" if existing_task is None else "task_updated",
                    summary=f"Task moved to {status}.",
                    status=status,
                    payload={"message_id": message.message_id},
                    created_at=now,
                )
            ]
        )
        steps = [
            TaskStepRecord(
                task_id=task.task_id,
                step_id=f"activity-{activity.activity_id}",
                workflow_id=workflow.workflow_id,
                run_id=run_id,
                step_kind=activity.activity_kind,
                description=activity.description,
                status="completed",
                approval_required=activity.approval_required,
                sequence_number=index,
                payload={},
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
            for index, activity in enumerate(plan.activities, start=1)
        ]
        self.run_store.upsert_task_steps(steps)
        return task

    def _record_task_thread_state(
        self,
        *,
        task: TaskRecord,
        thread_state: SaiEmailThreadState,
    ) -> None:
        updated_task = task.model_copy(
            update={
                "opaque_payload": {
                    **task.opaque_payload,
                    "thread_state": thread_state.model_dump(mode="json"),
                },
                "updated_at": utc_now(),
            }
        )
        self.run_store.upsert_task(updated_task)

    def _refresh_open_thread_messages(
        self,
        workflow_id: str,
        connector: GmailAPIConnector,
    ) -> list[Any]:
        refreshed: list[Any] = []
        for task in self.run_store.list_tasks(workflow_id=workflow_id, limit=100):
            if task.status not in OPEN_TASK_STATUSES or not task.source_thread_id:
                continue
            thread_state = task.opaque_payload.get("thread_state", {})
            last_processed_message_id = (
                str(thread_state.get("last_processed_message_id", "")).strip()
                if isinstance(thread_state, dict)
                else ""
            )
            messages = connector.fetch_thread_messages(thread_id=task.source_thread_id)
            if not messages:
                continue
            latest = self._latest_message_per_thread(messages)[0]
            if latest.message_id != last_processed_message_id:
                refreshed.append(latest)
        return refreshed

    def _find_open_task_for_thread(self, workflow_id: str, thread_id: str) -> TaskRecord | None:
        tasks = self.run_store.list_tasks_for_thread(
            workflow_id=workflow_id,
            source_thread_id=thread_id,
            statuses=list(OPEN_TASK_STATUSES),
            limit=5,
        )
        return tasks[0] if tasks else None

    def _filter_unprocessed_messages(self, workflow_id: str, messages: list[Any]) -> list[Any]:
        item_map = {
            item["item_id"]: item
            for item in self.run_store.list_workflow_items(workflow_id=workflow_id)
        }
        filtered: list[Any] = []
        for message in messages:
            item_id = message.thread_id or message.message_id
            existing = item_map.get(item_id)
            metadata = existing.get("metadata", {}) if existing else {}
            if metadata.get("last_processed_message_id") == message.message_id:
                continue
            filtered.append(message)
        return filtered

    @staticmethod
    def _latest_message_per_thread(messages: list[Any]) -> list[Any]:
        latest_by_thread: dict[str, Any] = {}
        for message in messages:
            key = message.thread_id or message.message_id
            existing = latest_by_thread.get(key)
            if existing is None:
                latest_by_thread[key] = message
                continue
            existing_time = existing.received_at or datetime.min.replace(tzinfo=UTC)
            current_time = message.received_at or datetime.min.replace(tzinfo=UTC)
            if current_time >= existing_time:
                latest_by_thread[key] = message
        return list(latest_by_thread.values())

    def _load_tool_prompts(self, workflow: WorkflowDefinition) -> dict[str, Any]:
        prompts: dict[str, Any] = {}
        for tool in workflow.tools:
            if tool.enabled and tool.prompt:
                prompts[tool.tool_id] = self.prompt_store.load(tool.prompt)
        return prompts

    def _enforce_connector_actions(
        self,
        *,
        run_id: str,
        workflow_id: str,
        policy: Any,
        actor: str,
        actions: list[Any],
    ) -> None:
        for action in actions:
            self.approval_service.enforce(
                run_id=run_id,
                workflow_id=workflow_id,
                policy=policy,
                action=action.action,
                actor=actor,
                reason=action.reason,
            )

    def _send_task_reply(
        self,
        *,
        run_id: str,
        workflow_id: str,
        policy: Any,
        message: Any,
        body: str,
    ) -> dict[str, Any]:
        self.audit_logger.append_event(
            run_id=run_id,
            workflow_id=workflow_id,
            actor="sai-email-interaction",
            component="email",
            event_type="reply.prepared",
            payload={"thread_id": message.thread_id, "message_id": message.message_id},
        )
        reply = self.sai_email_worker.send_thread_reply(
            policy=policy,
            to_email=self.sai_email_worker.resolve_reply_recipient(
                policy=policy,
                from_email=message.from_email,
            ),
            subject=message.subject,
            body=body,
            thread_id=message.thread_id,
        )
        self.audit_logger.append_event(
            run_id=run_id,
            workflow_id=workflow_id,
            actor="sai-email-interaction",
            component="email",
            event_type="reply.sent",
            payload=reply,
        )
        return reply

    def _write_artifact(self, *, run_id: str, payload: dict[str, Any]) -> str:
        artifact_dir = self.settings.artifacts_dir / run_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / "result.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return str(path)
