"""Simple consistency evaluator for starter email classification."""

from __future__ import annotations

from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import EmailClassification, EmailMessage


class ClassificationConsistencyEvaluatorTool:
    """Select the safest final classification from keyword/local/cloud candidates."""

    def __init__(self, *, tool_id: str) -> None:
        self.tool_id = tool_id

    def evaluate(
        self,
        *,
        message: EmailMessage,
        keyword_candidate: EmailClassification,
        local_candidate: EmailClassification | None,
        cloud_candidate: EmailClassification | None,
    ) -> tuple[EmailClassification, ToolExecutionRecord]:
        final_candidate = cloud_candidate or local_candidate or keyword_candidate
        decision_source = "cloud" if cloud_candidate else "local" if local_candidate else "keyword"

        if (
            keyword_candidate.level1_classification == "newsletters"
            and keyword_candidate.confidence >= 0.88
            and final_candidate.level1_classification != "newsletters"
            and final_candidate.confidence < 0.75
        ):
            final_candidate = keyword_candidate.model_copy(
                update={
                    "reason": (
                        f"{keyword_candidate.reason} Deterministic newsletter evidence "
                        "overrode a low-confidence model disagreement."
                    )[:240]
                }
            )
            decision_source = "keyword_override"

        if (
            final_candidate.level1_classification == "other"
            and _looks_like_actionable_message(message)
            and final_candidate.level2_intent == "informational"
        ):
            final_candidate = final_candidate.model_copy(
                update={
                    "level2_intent": "action_required",
                    "reason": (
                        f"{final_candidate.reason} The latest message contains a direct ask, "
                        "so the intent was upgraded to action_required."
                    )[:240],
                }
            )

        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="consistency_evaluator",
            status=ToolExecutionStatus.COMPLETED,
            details={
                "decision_source": decision_source,
                "keyword_level1": keyword_candidate.level1_classification,
                "local_level1": local_candidate.level1_classification if local_candidate else None,
                "cloud_level1": cloud_candidate.level1_classification if cloud_candidate else None,
                "final_level1": final_candidate.level1_classification,
                "final_level2": final_candidate.level2_intent,
            },
        )
        return final_candidate, record


def _looks_like_actionable_message(message: EmailMessage) -> bool:
    lowered = message.combined_text().lower()
    markers = ("please", "could you", "can you", "reply", "approve", "let me know")
    return any(marker in lowered for marker in markers)
