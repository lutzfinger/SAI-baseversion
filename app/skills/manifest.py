"""SkillManifest — the declarative contract every workflow ships.

PRINCIPLES.md §33 defines the protocol. This module is the Pydantic
schema. The loader (``app/skills/loader.py``) validates manifests
against this schema PLUS the file-system contract (eval files exist
+ meet min_count, etc.).

Hard contract (refused at load time):

  * Missing eval.canaries / eval.edge_cases / eval.workflow_regression
  * Tool with `propose_only` rights but no two-phase commit in policy
  * Tool with `mutate_with_approval` but `policy.approval_required` false
  * Side-effect output that is neither requires_approval, behind a `human`
    tier, nor pre_approved + a `second_opinion` tier
  * pre_approved side-effect output without a `second_opinion` safety tier

Soft contract (warnings, not refusal):

  * Cost cap > $1/invocation
  * Daily invocation cap > 1000
  * Vendor SDK names in tool inputs (suggests Provider abstraction missing)
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


# ─── identity ─────────────────────────────────────────────────────────


class SkillIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # PR 1a: accept `skill_id` as an input alias for `workflow_id`. The
    # cowork/claude_code skill-creator emits `skill_id`; PRINCIPLES §33 and
    # the rest of SAI use `workflow_id`. AliasChoices lets both parse into
    # the same canonical field so we don't fork on naming.
    workflow_id: str = Field(
        ..., min_length=3, max_length=80,
        validation_alias=AliasChoices("workflow_id", "skill_id"),
        description="Unique id (kebab-case). E.g. 'email-triage' or 'sai-eval-agent'.",
    )
    version: str = Field(..., description="Semver (e.g. 1.0.0).")
    owner: str = Field(..., description="User or team responsible (audit trail).")
    # max_length bumped 500 -> 4000 (PR 1a): real registered skills carry
    # richer multi-paragraph descriptions; the 500 cap was arbitrary.
    description: str = Field(..., min_length=10, max_length=4000)


# ─── trigger ──────────────────────────────────────────────────────────


TriggerKind = Literal[
    "email_pattern",   # Inbound email matches a Gmail query
    "schedule",        # cron-style
    "manual",          # operator-invoked CLI / Slack /sai-run
    "slack_message",   # top-level message in a channel
    "http_webhook",    # external POST
    "claude_tool",     # invoked by a Claude Co-Work skill via sai-run
]


class SkillTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TriggerKind
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Trigger-specific config. For email_pattern: "
            "{query: 'in:inbox newer_than:1d'}. For schedule: "
            "{cron: '0 7 * * *'}. For slack_message: {channel: 'sai-eval'}."
        ),
    )


# ─── cascade ──────────────────────────────────────────────────────────


TierKind = Literal[
    "rules", "classifier", "local_llm", "cloud_llm", "agent",
    "second_opinion", "human",
    # Added PR 1a (schema fits reality): real skills use a deterministic
    # rules gate that escalates to a local LLM only on ambiguity, and a
    # pure-LLM vision/OCR tier. Both are legitimate cascade kinds.
    "rules+llm", "llm",
]


class CascadeTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier_id: str
    kind: TierKind
    config: dict[str, Any] = Field(default_factory=dict)
    confidence_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0,
        description="Below this the cascade escalates to the next tier.",
    )
    cost_cap_per_call_usd: float = Field(
        default=0.10, ge=0.0,
        description="Hard cost cap per single invocation of this tier.",
    )


# ─── tools (only required if any tier is `agent`) ─────────────────────


ToolRights = Literal["read_only", "propose_only", "mutate_with_approval"]


class ToolDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: str
    rights: ToolRights = Field(
        ...,
        description=(
            "read_only: no mutation. propose_only: writes a YAML proposal "
            "for the operator's two-phase commit. mutate_with_approval: "
            "mutates state directly but only after policy.approval_required "
            "operator ✅."
        ),
    )
    blast_radius: str = Field(
        ..., min_length=10, max_length=400,
        description="One-paragraph description of what this tool can affect at worst.",
    )
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    input_check: str = Field(
        default="", description="What's validated server-side before invoke.",
    )
    output_check: str = Field(
        default="", description="What's validated server-side before return.",
    )


# ─── eval contract (mandatory) ────────────────────────────────────────
#
# Per PRINCIPLES.md §16a (revised 2026-05-03): every eval slot is an
# instance of EvalDataset. The manifest declares them as a discriminated
# union under ``eval.datasets``. Each dataset spec maps directly onto a
# ``EvalDataset`` subclass at load time (CanariesSpec → CanaryDataset,
# etc.).


FailMode = Literal["hard_fail", "soft_fail"]
EvalMetric = Literal["precision_recall", "accuracy", "f1"]


class _BaseDatasetSpec(BaseModel):
    """Common fields every dataset spec carries."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ..., description="Path relative to skill_dir (e.g. 'canaries.jsonl').",
    )
    min_count: int = Field(default=1, ge=0)


class CanariesSpec(_BaseDatasetSpec):
    kind: Literal["canaries"]
    fail_mode: FailMode = Field(default="hard_fail")
    min_count: int = Field(default=1, ge=1)


class EdgeCasesSpec(_BaseDatasetSpec):
    kind: Literal["edge_cases"]
    cap: Optional[int] = Field(default=50, ge=1)
    fail_mode: FailMode = Field(default="soft_fail")
    metric: EvalMetric = Field(default="precision_recall")
    max_p_r_drop: float = Field(default=0.10, ge=0.0, le=1.0)
    min_count: int = Field(default=5, ge=1)


class WorkflowSpec(_BaseDatasetSpec):
    kind: Literal["workflow"]
    fail_mode: FailMode = Field(default="hard_fail")
    min_count: int = Field(default=5, ge=1)


class TrueNorthSpec(_BaseDatasetSpec):
    kind: Literal["true_north"]
    cap: None = None  # always uncapped — that's the principle
    fail_mode: FailMode = Field(default="soft_fail")
    run_cadence: Literal["weekly", "manual"] = Field(default="manual")
    max_cost_per_run_usd: float = Field(default=2.00, ge=0.0)
    min_count: int = Field(default=0, ge=0)


class DisagreementQueueSpec(_BaseDatasetSpec):
    kind: Literal["disagreement_queue"]
    fail_mode: FailMode = Field(default="soft_fail")
    min_count: int = Field(default=0, ge=0)


# Discriminated union — pydantic dispatches by the `kind` literal.
DatasetSpec = Annotated[
    Union[
        CanariesSpec, EdgeCasesSpec, WorkflowSpec,
        TrueNorthSpec, DisagreementQueueSpec,
    ],
    Field(discriminator="kind"),
]


class SkillEval(BaseModel):
    """Workflow's eval contract — a list of datasets.

    Required kinds (validated by the loader): canaries, edge_cases,
    workflow. Optional: true_north, disagreement_queue. Every kind
    can appear at most once per workflow.
    """

    model_config = ConfigDict(extra="forbid")

    datasets: list[DatasetSpec] = Field(..., min_length=3)

    # Convenience accessors (loader uses these for the hard contract
    # check). Returns None if the kind isn't declared.
    def get(self, kind: str) -> Optional[_BaseDatasetSpec]:
        for d in self.datasets:
            if d.kind == kind:
                return d
        return None

    def kinds_present(self) -> set[str]:
        return {d.kind for d in self.datasets}


# ─── feedback ─────────────────────────────────────────────────────────


class SkillFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str = Field(default="sai-eval")
    patterns: list[str] = Field(
        default_factory=lambda: ["add_rule", "eval_add"],
        description="Pre-registered patterns (PRINCIPLES.md §16b).",
    )


# ─── outputs ──────────────────────────────────────────────────────────


SideEffect = Literal[
    "label", "reply", "draft", "send", "post", "propose", "none",
    # Added 2026-05-27 (PR 1a — schema fits reality): real skills produce
    # side effects the original email/slack-centric literal didn't cover.
    #   local_write   — atomic skills writing local files (transcripts, manifests)
    #   file_write    — bookkeeping-rules / log files written during a run
    #   external_write — QuickBooks Purchases/Invoices and other external mutations
    "local_write", "file_write", "external_write",
]


class SkillOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    side_effect: SideEffect
    requires_approval: bool = Field(
        default=False,
        description="If true, output goes through two-phase commit (#9).",
    )
    pre_approved: bool = Field(
        default=False,
        description=(
            "First-class 'pre-approved per skill sign-off' posture: the operator "
            "approved this side effect ONCE when approving the skill, so it runs "
            "without per-run approval. Mirrors the registry approval_behavior "
            "'preapproved_per_skill_signoff'. The loader REQUIRES a `second_opinion` "
            "cascade tier whenever this is true (a different-LLM safety gate)."
        ),
    )


# ─── policy ───────────────────────────────────────────────────────────


class SkillPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_required: bool = Field(default=False)
    cost_cap_per_invocation_usd: float = Field(default=0.10, ge=0.0)
    iteration_cap: int = Field(default=8, ge=1, le=100)
    daily_invocation_cap: int = Field(default=100, ge=1)
    audit_log_path: str = Field(
        default="~/Library/Logs/SAI/{workflow_id}.jsonl",
    )


# ─── observability ────────────────────────────────────────────────────


class SkillObservability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    langsmith_project: Optional[str] = None
    metrics_emit: bool = Field(default=True)


# ─── the manifest ─────────────────────────────────────────────────────


class SkillManifest(BaseModel):
    """The single declarative contract a SAI skill ships.

    Loaded from ``skill.yaml``. Validated by the loader. If validation
    passes, the skill is registered + can run; if it fails, the
    framework refuses to register it (#6 fail closed).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = Field(
        default="1",
        description="Bump this when the manifest schema changes incompatibly.",
    )
    identity: SkillIdentity
    trigger: SkillTrigger
    cascade: list[CascadeTier] = Field(..., min_length=1)
    # PR 1a: tools may be bare registry-id strings OR full ToolDeclaration
    # dicts. ToolDeclaration is tried first so a full dict isn't stringified.
    tools: list[Union[ToolDeclaration, str]] = Field(default_factory=list)
    eval: SkillEval
    feedback: SkillFeedback = Field(default_factory=SkillFeedback)
    outputs: list[SkillOutput] = Field(..., min_length=1)
    policy: SkillPolicy = Field(default_factory=SkillPolicy)
    observability: SkillObservability = Field(default_factory=SkillObservability)
    # PR 1a: optional declarative metadata real skills already carry.
    auth_preconditions: list[str] = Field(default_factory=list)
    overlay_required: list[str] = Field(default_factory=list)
    # Provenance is optional metadata (who designed it, when). Its mere
    # presence does NOT make a skill a candidate — only failing the schema
    # while carrying provenance does (see manifest_validator).
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("cascade")
    @classmethod
    def _cascade_has_unique_tier_ids(cls, v: list[CascadeTier]) -> list[CascadeTier]:
        ids = [t.tier_id for t in v]
        if len(ids) != len(set(ids)):
            raise ValueError(f"cascade tier_ids must be unique; got {ids}")
        return v

    @field_validator("tools")
    @classmethod
    def _tool_ids_unique(
        cls, v: list[Union[ToolDeclaration, str]]
    ) -> list[Union[ToolDeclaration, str]]:
        # A tool entry is either a bare string id or a ToolDeclaration.
        ids = [t if isinstance(t, str) else t.tool_id for t in v]
        if len(ids) != len(set(ids)):
            raise ValueError(f"tool_ids must be unique; got {ids}")
        return v


# ─── validation result types ──────────────────────────────────────────


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning"]
    rule: str
    message: str


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return f"{self.workflow_id}: validates clean."
        parts = [f"{self.workflow_id}:"]
        for issue in self.errors:
            parts.append(f"  ❌ {issue.rule}: {issue.message}")
        for issue in self.warnings:
            parts.append(f"  ⚠ {issue.rule}: {issue.message}")
        return "\n".join(parts)


# ─── v2 schema: multi-profile skill manifest ─────────────────────────
#
# See ~/Lutz_Dev/SAI/docs/PLAN-UNIFIED-SKILL-SYNC.md for rationale.
#
# v1 (above) = single SAI workflow per skill.yaml. v2 = one skill_id can
# carry multiple deliverables (e.g., a granola-fetch SAI workflow runner
# AND a granola-fetch Claude Code SKILL.md prose form). Each deliverable
# is a "profile" with its own files[], deploy_to[], and eval contract.
#
# Backward-compat: a skill.yaml with schema_version="1" (or no profiles:
# key) is read as an implicit single sai_workflow profile.

import re as _re_v2  # avoid colliding with any future top-level `re` import


DeployTarget = Literal["sai_runtime", "claude_code", "cowork"]


CLAUDE_CODE_SUBDIR_RE = _re_v2.compile(r"^[A-Z][A-Za-z0-9_-]{0,30}$")


class SaiWorkflowProfile(BaseModel):
    """v2 sai_workflow profile — wraps the v1 SkillManifest body.

    Carries the same #33 slots (trigger/cascade/tools/eval/outputs/policy)
    plus the v2-only declarative additions (`files`, `deploy_to`).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True)
    files: list[str] = Field(default_factory=list)
    deploy_to: list[DeployTarget] = Field(
        default_factory=lambda: ["sai_runtime"],
        description=(
            "Which targets the deploy CLI should write this profile to. "
            "sai_runtime is the default for SAI workflows; cowork would "
            "rarely make sense and claude_code is for runner.py-as-CLI "
            "edge cases."
        ),
    )

    # The full #33 contract — same shapes as the v1 SkillManifest.
    trigger: SkillTrigger
    cascade: list[CascadeTier] = Field(..., min_length=1)
    tools: list[Union[ToolDeclaration, str]] = Field(default_factory=list)
    eval: SkillEval
    feedback: SkillFeedback = Field(default_factory=SkillFeedback)
    outputs: list[SkillOutput] = Field(..., min_length=1)
    policy: SkillPolicy = Field(default_factory=SkillPolicy)
    observability: SkillObservability = Field(default_factory=SkillObservability)

    @field_validator("cascade")
    @classmethod
    def _cascade_has_unique_tier_ids(cls, v: list[CascadeTier]) -> list[CascadeTier]:
        ids = [t.tier_id for t in v]
        if len(ids) != len(set(ids)):
            raise ValueError(f"cascade tier_ids must be unique; got {ids}")
        return v

    @field_validator("deploy_to")
    @classmethod
    def _deploy_to_unique(cls, v: list[DeployTarget]) -> list[DeployTarget]:
        if len(v) != len(set(v)):
            raise ValueError(f"deploy_to entries must be unique; got {v}")
        return v


class ClaudeCodeEval(BaseModel):
    """Eval contract for a claude_code profile.

    Lighter than SkillEval — only canaries are required for Claude Code
    skills (no cascade exists to need edge_cases/workflow datasets).
    """

    model_config = ConfigDict(extra="forbid")

    datasets: list[CanariesSpec] = Field(..., min_length=1)


class ClaudeCodeProfile(BaseModel):
    """v2 claude_code profile — prose SKILL.md + assets, no cascade.

    Reject cascade / tools / outputs / policy: those belong to
    sai_workflow only. A Claude Code skill is invoked by Claude reading
    SKILL.md, not by the SAI cascade machinery.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True)
    files: list[str] = Field(default_factory=list)
    deploy_to: list[DeployTarget] = Field(
        default_factory=lambda: ["claude_code"],
    )
    claude_code_subdir: Optional[str] = Field(
        default=None,
        description=(
            "If set, deploy claude_code profile under "
            "~/.claude/skills/<subdir>/<skill_id>/ instead of "
            "~/.claude/skills/<skill_id>/. Avoids namespace collision "
            "with the anthropic-skills plugin install."
        ),
    )
    eval: ClaudeCodeEval

    @field_validator("claude_code_subdir")
    @classmethod
    def _subdir_shape(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not CLAUDE_CODE_SUBDIR_RE.match(v):
            raise ValueError(
                f"claude_code_subdir must match {CLAUDE_CODE_SUBDIR_RE.pattern!r}; "
                f"got {v!r} (no path chars, no traversal, starts with uppercase)"
            )
        return v

    @field_validator("deploy_to")
    @classmethod
    def _deploy_to_unique_cc(cls, v: list[DeployTarget]) -> list[DeployTarget]:
        if len(v) != len(set(v)):
            raise ValueError(f"deploy_to entries must be unique; got {v}")
        return v


class SkillManifestV2(BaseModel):
    """v2 manifest — multi-profile container.

    A skill.yaml carries at most one profile of each kind. Both profiles
    enabled means the same skill_id ships two deliverables (the
    granola-fetch pattern).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"]
    identity: SkillIdentity
    profiles: "SkillProfiles"
    # Optional designer-provenance metadata (see SkillManifest.provenance).
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("profiles")
    @classmethod
    def _at_least_one_enabled(cls, v: "SkillProfiles") -> "SkillProfiles":
        if not (
            (v.sai_workflow is not None and v.sai_workflow.enabled)
            or (v.claude_code is not None and v.claude_code.enabled)
        ):
            raise ValueError(
                "at least one profile must be enabled (sai_workflow or claude_code)"
            )
        return v


class SkillProfiles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sai_workflow: Optional[SaiWorkflowProfile] = None
    claude_code: Optional[ClaudeCodeProfile] = None


# Forward-ref resolve (SkillProfiles defined after SkillManifestV2 reference).
SkillManifestV2.model_rebuild()
