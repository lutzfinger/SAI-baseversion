"""Shared email schemas for the starter SAI repo."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord

# Two-layer email taxonomy used by the starter classification prompts and the
# Gmail-tagging workflow. Customize these values in your private overlay to
# match how you actually sort email — the prompts and policies must agree.
#
#   Level 1 — relationship/context bucket. The prompt template marks the L1
#             list as `# CUSTOMIZE`; this Literal must stay aligned with it.
#   Level 2 — intent. Universal vocabulary, rarely changed.
#
# `other` and `others` are unresolved fallbacks: they keep the email in inbox
# and produce no L1/L2 label.
Level1Classification = Literal[
    "customers",
    "partners",
    "job_hunt",
    "personal",
    "friends",
    "admin_finance",
    "newsletters",
    "updates",
    "other",
]
Level2Intent = Literal[
    "informational",
    "action_required",
    "others",
]

LEVEL1_DISPLAY_NAMES: dict[str, str] = {
    "customers": "Customers",
    "partners": "Partners",
    "job_hunt": "Job Hunt",
    "personal": "Personal",
    "friends": "Friends",
    "admin_finance": "Finance",
    "newsletters": "Newsletters",
    "updates": "Updates",
}

LEVEL2_DISPLAY_NAMES: dict[str, str] = {
    "informational": "Informational",
    "action_required": "Action Required",
}

# Gmail label names produced by the L1/L2 taxonomy.
PRIMARY_LABELS: dict[str, str] = {
    key: f"L1/{display}" for key, display in LEVEL1_DISPLAY_NAMES.items()
}
INTENT_LABELS: dict[str, str] = {
    key: f"L2/{display}" for key, display in LEVEL2_DISPLAY_NAMES.items()
}
STARTER_INPUT_LABEL = "Starter/Input"

# Back-compat aliases — prefer Level1Classification / Level2Intent in new code.
PrimaryCategory = Level1Classification
SecondaryIntent = Level2Intent


class EmailMessage(BaseModel):
    """Minimal read-safe message shape used across Gmail-connected workflows."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    thread_id: str | None = None
    from_email: str
    from_name: str | None = None
    to: list[str]
    cc: list[str] = Field(default_factory=list)
    delivered_to: list[str] = Field(default_factory=list)
    subject: str
    snippet: str
    body_excerpt: str = ""
    list_unsubscribe: list[str] = Field(default_factory=list)
    list_unsubscribe_post: str | None = None
    unsubscribe_links: list[str] = Field(default_factory=list)
    received_at: datetime | None = None

    def combined_text(self) -> str:
        parts = [self.subject.strip(), self.snippet.strip(), self.body_excerpt.strip()]
        return " ".join(part for part in parts if part).strip()


class EmailAttachmentText(BaseModel):
    """Bounded text extracted from one safe email attachment."""

    model_config = ConfigDict(extra="forbid")

    filename: str | None = None
    mime_type: str
    text: str
    extraction_method: str


class EmailDocument(BaseModel):
    """Richer email document with bounded attachment text."""

    model_config = ConfigDict(extra="forbid")

    message: EmailMessage
    plain_text: str = ""
    html_text: str = ""
    attachment_texts: list[EmailAttachmentText] = Field(default_factory=list)

    def combined_text(self) -> str:
        parts = [self.message.combined_text(), self.plain_text.strip()]
        parts.extend(item.text.strip() for item in self.attachment_texts if item.text.strip())
        return "\n\n".join(part for part in parts if part).strip()


class EmailClassification(BaseModel):
    """Generic starter classification with a toned-down two-part structure."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    level1_classification: Level1Classification
    level2_intent: Level2Intent
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str

    def gmail_label_names(self) -> list[str]:
        return gmail_label_names_for_classification(self)


class EmailThreadTagRequest(BaseModel):
    """Strict input for thread tagging based on starter classification."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    classification: EmailClassification
    archive_from_inbox: bool = True

    def gmail_label_names(self) -> list[str]:
        return self.classification.gmail_label_names()


class EmailThreadTagResult(BaseModel):
    """Structured output for Gmail thread labeling."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    applied_label_names: list[str] = Field(default_factory=list)
    applied_label_ids: list[str] = Field(default_factory=list)
    removed_label_names: list[str] = Field(default_factory=list)
    removed_label_ids: list[str] = Field(default_factory=list)
    created_label_names: list[str] = Field(default_factory=list)
    created_label_ids: list[str] = Field(default_factory=list)
    archived_from_inbox: bool = False


class GmailThreadLabelRequest(BaseModel):
    """Generic Gmail label mutation request."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    label_names: list[str] = Field(default_factory=list)
    remove_label_names: list[str] = Field(default_factory=list)
    archive_from_inbox: bool = False
    clear_taxonomy_labels: bool = False


class GmailThreadLabelResult(BaseModel):
    """Result for generic Gmail label operations."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    applied_label_names: list[str] = Field(default_factory=list)
    applied_label_ids: list[str] = Field(default_factory=list)
    removed_label_names: list[str] = Field(default_factory=list)
    removed_label_ids: list[str] = Field(default_factory=list)
    created_label_names: list[str] = Field(default_factory=list)
    created_label_ids: list[str] = Field(default_factory=list)
    archived_from_inbox: bool = False


class EmailTriageArtifact(BaseModel):
    """Persisted artifact for one starter email triage run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    items: list[EmailClassification]


class EmailTriageResult(BaseModel):
    """One message-level starter email triage result."""

    model_config = ConfigDict(extra="forbid")

    classification: EmailClassification
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)
    tag_result: EmailThreadTagResult | None = None


def gmail_label_names_for_classification(classification: EmailClassification) -> list[str]:
    names: list[str] = []
    if classification.level1_classification in PRIMARY_LABELS:
        names.append(PRIMARY_LABELS[classification.level1_classification])
    if classification.level2_intent in INTENT_LABELS:
        names.append(INTENT_LABELS[classification.level2_intent])
    return names


def all_taxonomy_gmail_label_names() -> list[str]:
    return [*PRIMARY_LABELS.values(), *INTENT_LABELS.values()]


def gmail_level1_label_name(level1_classification: Level1Classification) -> str | None:
    """Return the Gmail label for a resolved L1 classification, or None for fallbacks."""

    if level1_classification in {"newsletters", "other"}:
        return None
    return PRIMARY_LABELS.get(level1_classification)


def starter_input_label_names() -> list[str]:
    return [STARTER_INPUT_LABEL]


class EmailDatasetExample(EmailMessage):
    """Dataset record for fixtures, evals, and learning corpora.

    Extends the live `EmailMessage` schema with an optional stable label so
    historical datasets can carry the same core email fields as the runtime
    connector while still tracking where each example came from.
    """

    body: str | None = None
    source_label: str | None = None


class LabeledEmailDatasetExample(EmailDatasetExample):
    """Dataset row with expected L1/L2 labels for offline evaluation."""

    expected_level1_classification: Level1Classification
    expected_level2_intent: Level2Intent
    raw_level1_label: str | None = None
    raw_level2_label: str | None = None


def build_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None = None,
    items: list[EmailClassification],
) -> EmailTriageArtifact:
    return EmailTriageArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        items=items,
    )
