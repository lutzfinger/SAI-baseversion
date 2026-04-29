"""Schemas for governed travel-operation planning and execution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import EmailMessage

TravelPlanningStatus = Literal["planned", "skipped_non_travel", "guard_blocked", "failed"]
TravelExecutionStatus = Literal[
    "completed",
    "needs_operator_confirmation",
    "failed",
]


class TravelAttachmentText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str | None = None
    mime_type: str
    text: str
    extraction_method: Literal["text_part", "html_part", "image_ocr", "filename_only"]


class TravelEmailDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: EmailMessage
    plain_text: str = ""
    html_text: str = ""
    attachment_texts: list[TravelAttachmentText] = Field(default_factory=list)

    def combined_text(self) -> str:
        parts = [
            self.message.subject.strip(),
            self.message.snippet.strip(),
            self.plain_text.strip(),
            self.html_text.strip() if not self.plain_text.strip() else "",
            *(attachment.text.strip() for attachment in self.attachment_texts if attachment.text),
        ]
        return "\n\n".join(part for part in parts if part).strip()


class TravelCostFilingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    booking_email_query: str
    airline_hint: str | None = None
    selection_rule: str
    quickbooks_note: str


class TravelCalendarPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    create_flight_calendar_entry: bool = True
    event_title: str
    event_timezone: str
    delete_prior_event_query: str | None = None
    delete_window_start: str | None = None
    delete_window_end: str | None = None


class TravelRoutePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prior_location_hint: str | None = None
    fallback_question: str
    airport_name: str
    airport_code: str | None = None
    transport_query: str


class TravelOperationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    safe_for_work: bool = True
    content_rating: Literal["g", "pg"] = "g"
    request_summary: str
    execution_steps: list[str] = Field(default_factory=list)
    cost_filing: TravelCostFilingPlan
    calendar_action: TravelCalendarPlan
    route_action: TravelRoutePlan
    missing_information: list[str] = Field(default_factory=list)
    critical_assumptions: list[str] = Field(default_factory=list)
    rationale: str


class TravelPlanCritique(BaseModel):
    model_config = ConfigDict(extra="forbid")

    safe_for_work: bool = True
    content_rating: Literal["g", "pg"] = "g"
    should_revise: bool = False
    issues: list[str] = Field(default_factory=list)
    risky_assumptions: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)


class TravelItineraryExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    airline: str
    booking_reference: str | None = None
    passenger_name: str | None = None
    departure_airport_code: str
    departure_airport_name: str | None = None
    arrival_airport_code: str
    arrival_airport_name: str | None = None
    departure_local_iso: str
    arrival_local_iso: str
    departure_timezone: str
    arrival_timezone: str
    total_cost_amount: float | None = None
    currency: str | None = None
    evidence_summary: str


class TravelRouteResearchReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    safe_for_work: bool = True
    content_rating: Literal["g", "pg"] = "g"
    origin: str
    destination: str
    recommended_transport_mode: str
    estimated_duration_minutes: int
    recommended_departure_buffer_minutes: int
    recommended_security_buffer_minutes: int
    recommendation_summary: str
    source_urls: list[str] = Field(default_factory=list)


class TravelExecutionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    safe_for_work: bool = True
    content_rating: Literal["g", "pg"] = "g"
    verdict: Literal["good", "needs_attention"]
    summary: str
    strengths: str
    improvements: str


class TravelOperationPlanningItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_message: EmailMessage
    request_document: TravelEmailDocument | None = None
    status: TravelPlanningStatus
    plan: TravelOperationPlan | None = None
    critique: TravelPlanCritique | None = None
    revised_plan: TravelOperationPlan | None = None
    approval_question: str | None = None
    approval_request_id: str | None = None
    slack_question_id: str | None = None
    success_post_channel: str | None = None
    success_post_ts: str | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)
    error: str | None = None


class TravelOperationPlanningResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewed_message_count: int = 0
    planned_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    items: list[TravelOperationPlanningItem] = Field(default_factory=list)
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class TravelOperationExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TravelExecutionStatus
    instruction_message_reference: str
    filed_cost: bool = False
    booking_message_id: str | None = None
    forwarded_message_id: str | None = None
    deleted_event_ids: list[str] = Field(default_factory=list)
    created_event_ids: list[str] = Field(default_factory=list)
    route_origin: str | None = None
    route_report: TravelRouteResearchReport | None = None
    itinerary: TravelItineraryExtraction | None = None
    gemini_review: TravelExecutionReview | None = None
    review_reply_channel: str | None = None
    review_reply_thread_ts: str | None = None
    operator_question_text: str | None = None
    error: str | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class TravelOperationPlanningArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    result: TravelOperationPlanningResult


class TravelOperationExecutionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    result: TravelOperationExecutionResult


def build_travel_operation_planning_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None,
    result: TravelOperationPlanningResult,
) -> TravelOperationPlanningArtifact:
    return TravelOperationPlanningArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        result=result,
    )


def build_travel_operation_execution_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None,
    result: TravelOperationExecutionResult,
) -> TravelOperationExecutionArtifact:
    return TravelOperationExecutionArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        result=result,
    )
