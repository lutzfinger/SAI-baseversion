"""Worker for starter newsletter identification."""

from __future__ import annotations

from app.connectors.gmail_labels import GmailLabelConnector
from app.learning.email_eval_dataset import EmailEvalDatasetStore, EmailEvalRecord, utc_now
from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.evaluator import ClassificationConsistencyEvaluatorTool
from app.tools.gmail_thread_tagger import GmailThreadTaggerTool
from app.tools.keyword_classifier import KeywordEmailClassifierTool
from app.tools.local_llm_classifier import StructuredEmailClassifierTool
from app.workers.email_models import EmailMessage
from app.workers.newsletter_identifier_models import (
    NewsletterIdentifierItem,
    NewsletterIdentifierResult,
)


class NewsletterIdentifierWorker:
    """Run deterministic + local + cloud newsletter identification."""

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings
        self.eval_store = EmailEvalDatasetStore(settings.newsletter_eval_dataset_path)

    def classify_messages(
        self,
        *,
        workflow_id: str,
        run_id: str,
        messages: list[EmailMessage],
        prompts_by_tool_id: dict[str, PromptDocument],
        tool_definitions: list[WorkflowToolDefinition],
        operator_email: str,
        label_connector: GmailLabelConnector | None = None,
        tagging_enabled: bool = False,
    ) -> NewsletterIdentifierResult:
        items: list[NewsletterIdentifierItem] = []
        tool_by_kind = {tool.kind: tool for tool in tool_definitions if tool.enabled}
        for message in messages:
            keyword_tool = KeywordEmailClassifierTool(
                tool_id=tool_by_kind["keyword_classifier"].tool_id,
                classifier_config=dict(
                    prompts_by_tool_id[tool_by_kind["keyword_classifier"].tool_id].config[
                        "classifier"
                    ]
                ),
            )
            keyword_candidate, keyword_record = keyword_tool.classify(
                message=message,
                operator_email=operator_email,
            )
            local_candidate = None
            cloud_candidate = None
            tool_records = [keyword_record]

            local_tool_def = tool_by_kind.get("local_llm_classifier")
            if local_tool_def is not None:
                local_tool = StructuredEmailClassifierTool(
                    tool_definition=local_tool_def,
                    prompt=prompts_by_tool_id[local_tool_def.tool_id],
                    settings=self.settings,
                )
                local_payload, local_record = local_tool.classify(
                    message_payload=message.model_dump(mode="json"),
                    operator_email=operator_email,
                    keyword_baseline=keyword_candidate,
                )
                tool_records.append(local_record)
                if local_payload is not None:
                    local_candidate = type(keyword_candidate).model_validate(local_payload)

            cloud_tool_def = tool_by_kind.get("cloud_llm_classifier")
            should_escalate = cloud_tool_def is not None and (
                local_candidate is None
                or local_candidate.confidence < 0.75
                or (
                    local_candidate.level1_classification != keyword_candidate.level1_classification
                )
            )
            if should_escalate and cloud_tool_def is not None:
                cloud_tool = StructuredEmailClassifierTool(
                    tool_definition=cloud_tool_def,
                    prompt=prompts_by_tool_id[cloud_tool_def.tool_id],
                    settings=self.settings,
                )
                cloud_payload, cloud_record = cloud_tool.classify(
                    message_payload=message.model_dump(mode="json"),
                    operator_email=operator_email,
                    keyword_baseline=local_candidate or keyword_candidate,
                )
                tool_records.append(cloud_record)
                if cloud_payload is not None:
                    cloud_candidate = type(keyword_candidate).model_validate(cloud_payload)

            evaluator = ClassificationConsistencyEvaluatorTool(
                tool_id=tool_by_kind["consistency_evaluator"].tool_id,
            )
            final_classification, evaluator_record = evaluator.evaluate(
                message=message,
                keyword_candidate=keyword_candidate,
                local_candidate=local_candidate,
                cloud_candidate=cloud_candidate,
            )
            tool_records.append(evaluator_record)

            tag_result = None
            if tagging_enabled and label_connector is not None and message.thread_id:
                tagger_tool = GmailThreadTaggerTool(
                    tool_id=tool_by_kind["gmail_thread_tagger"].tool_id,
                    connector=label_connector,
                )
                tag_result, tagger_record = tagger_tool.tag_thread(
                    thread_id=message.thread_id,
                    classification=final_classification,
                    archive_from_inbox=True,
                )
                tool_records.append(tagger_record)

            self.eval_store.append_record(
                EmailEvalRecord(
                    recorded_at=utc_now(),
                    workflow_id=workflow_id,
                    run_id=run_id,
                    message_id=message.message_id,
                    thread_id=message.thread_id,
                    subject=message.subject,
                    from_email=message.from_email,
                    predicted_level1=final_classification.level1_classification,
                    predicted_level2=final_classification.level2_intent,
                    confidence=final_classification.confidence,
                    reason=final_classification.reason,
                )
            )
            items.append(
                NewsletterIdentifierItem(
                    message=message,
                    classification=final_classification,
                    tool_records=tool_records,
                    tag_result=tag_result,
                )
            )

        return NewsletterIdentifierResult(
            reviewed_message_count=len(messages),
            classified_message_count=len(items),
            newsletter_message_count=sum(
                1 for item in items
                if item.classification.level1_classification == "newsletters"
            ),
            tagged_thread_count=sum(1 for item in items if item.tag_result is not None),
            items=items,
        )
