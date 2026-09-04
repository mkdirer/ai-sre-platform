"""Strict contracts for evidence-grounded AI investigation and reporting."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from packages.models.evidence import EvidenceId, EvidenceService, TimelineEvent
from packages.models.incidents import IncidentId, IncidentSeverity

HypothesisId = Annotated[str, StringConstraints(pattern=r"^HYP-[A-F0-9]{24}$")]
ReportId = Annotated[str, StringConstraints(pattern=r"^RPT-[A-F0-9]{24}$")]
RecommendationId = Annotated[str, StringConstraints(pattern=r"^REC-[A-F0-9]{24}$")]
GroundedText = Annotated[str, StringConstraints(min_length=1, max_length=512)]


class RootCauseCategory(StrEnum):
    """Closed causal taxonomy supported by deterministic evidence validators."""

    DATABASE_LATENCY = "database_latency"
    DB_POOL_SATURATION = "db_pool_saturation"
    BAD_DEPLOYMENT = "bad_deployment"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    CPU_SATURATION = "cpu_saturation"
    APPLICATION_ERRORS = "application_errors"


class HypothesisStatus(StrEnum):
    """Lifecycle states for a candidate explanation."""

    PROPOSED = "proposed"
    VERIFIED = "verified"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class AdditionalEvidenceKind(StrEnum):
    """Additional read-only operations the model may request by an evidence anchor."""

    LOGS_AROUND_EVIDENCE = "logs_around_evidence"
    TRACE_BY_ID_FROM_EVIDENCE = "trace_by_id_from_evidence"


class RecommendationAction(StrEnum):
    """Closed recommendation set; execution is deliberately absent in Stage 06."""

    NO_ACTION = "no_action"
    INVESTIGATE_DATABASE = "investigate_database"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"


class RecommendationRisk(StrEnum):
    """Bounded qualitative risk labels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReportStatus(StrEnum):
    """Terminal workflow result or durable human-approval pause."""

    COMPLETE = "complete"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    WAITING_FOR_APPROVAL = "waiting_for_approval"


class ModelOperation(StrEnum):
    """Logical model calls used for routing, budgets, and observability."""

    GENERATE_HYPOTHESES = "generate_hypotheses"
    VERIFY_HYPOTHESIS = "verify_hypothesis"
    SYNTHESIZE_REPORT = "synthesize_report"


class AdditionalEvidenceRequest(BaseModel):
    """Model-selected operation whose concrete parameters come from canonical evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AdditionalEvidenceKind
    service: EvidenceService
    anchor_evidence_id: EvidenceId
    reason: GroundedText


class HypothesisCandidate(BaseModel):
    """Schema-valid candidate proposed by the model before independent verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: RootCauseCategory
    description: GroundedText
    initial_evidence_ids: Annotated[list[EvidenceId], Field(max_length=12)]
    next_evidence_requests: Annotated[list[AdditionalEvidenceRequest], Field(max_length=2)] = Field(
        default_factory=list
    )


class HypothesisCandidates(BaseModel):
    """Structured output for competing candidate generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypotheses: Annotated[list[HypothesisCandidate], Field(min_length=1, max_length=5)]


class HypothesisVerification(BaseModel):
    """Structured model assessment later constrained by deterministic grounding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: HypothesisStatus
    confidence: Annotated[float, Field(ge=0, le=1)]
    supporting_evidence_ids: Annotated[list[EvidenceId], Field(max_length=20)]
    contradicting_evidence_ids: Annotated[list[EvidenceId], Field(max_length=20)]
    reasoning_summary: GroundedText
    next_evidence_requests: Annotated[list[AdditionalEvidenceRequest], Field(max_length=2)] = Field(
        default_factory=list
    )

    def model_post_init(self, _context: object) -> None:
        if set(self.supporting_evidence_ids) & set(self.contradicting_evidence_ids):
            raise ValueError("supporting and contradicting evidence must be disjoint")


class Hypothesis(BaseModel):
    """Canonical persisted hypothesis after deterministic verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: HypothesisId
    incident_id: IncidentId
    category: RootCauseCategory
    description: GroundedText
    status: HypothesisStatus
    confidence: Annotated[float, Field(ge=0, le=1)]
    supporting_evidence_ids: Annotated[list[EvidenceId], Field(max_length=20)]
    contradicting_evidence_ids: Annotated[list[EvidenceId], Field(max_length=20)]
    reasoning_summary: GroundedText
    next_evidence_requests: Annotated[list[AdditionalEvidenceRequest], Field(max_length=2)] = Field(
        default_factory=list
    )


class RecommendationProposal(BaseModel):
    """Model proposal with no arbitrary command or free-form parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_type: RecommendationAction
    target: EvidenceService
    rationale_evidence_ids: Annotated[list[EvidenceId], Field(max_length=12)]
    risk: RecommendationRisk
    reversible: bool


class ReportSynthesis(BaseModel):
    """Narrow model output used to select a validated hypothesis and recommendations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_hypothesis_id: HypothesisId | None
    recommendations: Annotated[list[RecommendationProposal], Field(max_length=3)]


class Recommendation(BaseModel):
    """Canonical recommendation; Stage 06 persists but never executes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: RecommendationId
    action_type: RecommendationAction
    target: EvidenceService
    parameters: dict[str, object]
    rationale_evidence_ids: Annotated[list[EvidenceId], Field(max_length=12)]
    risk: RecommendationRisk
    reversible: bool
    requires_approval: bool
    status: Annotated[str, StringConstraints(pattern=r"^(proposed|waiting_for_approval)$")]


class IncidentReport(BaseModel):
    """Evidence-grounded report assembled and validated by deterministic code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ReportId
    incident_id: IncidentId
    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    affected_services: Annotated[list[EvidenceService], Field(min_length=1, max_length=32)]
    severity: IncidentSeverity
    summary: GroundedText
    root_cause: RootCauseCategory | None
    root_cause_summary: GroundedText | None
    confidence: Annotated[float, Field(ge=0, le=1)]
    timeline: Annotated[list[TimelineEvent], Field(max_length=100)]
    hypotheses: Annotated[list[Hypothesis], Field(max_length=5)]
    evidence_references: Annotated[list[EvidenceId], Field(max_length=100)]
    recommendations: Annotated[list[Recommendation], Field(max_length=3)]
    related_incident_ids: Annotated[list[IncidentId], Field(max_length=20)] = Field(
        default_factory=list
    )
    limitations: Annotated[list[GroundedText], Field(max_length=20)]
    status: ReportStatus
    generated_at: datetime

    @field_validator("generated_at")
    @classmethod
    def normalize_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("report timestamp must include a timezone")
        return value.astimezone(UTC)

    def model_post_init(self, _context: object) -> None:
        if self.root_cause is None:
            if self.root_cause_summary is not None or self.confidence != 0:
                raise ValueError("a null root cause must have no summary and zero confidence")
        elif self.root_cause_summary is None:
            raise ValueError("a selected root cause requires a summary")
        requires_approval = any(item.requires_approval for item in self.recommendations)
        if requires_approval != (self.status == ReportStatus.WAITING_FOR_APPROVAL):
            raise ValueError("report approval status must match mutating recommendations")


class HypothesisPage(BaseModel):
    """Newest-run hypothesis page exposed by Incident API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[Hypothesis]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0)]


class RecommendationPage(BaseModel):
    """Newest-run recommendation page exposed by Incident API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[Recommendation]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0)]


class ModelCallRecord(BaseModel):
    """Secret-free metadata for one bounded provider or tool attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(pattern=r"^CALL-[A-F0-9]{24}$")]
    run_id: str
    incident_id: IncidentId
    kind: Annotated[str, StringConstraints(pattern=r"^(model|tool)$")]
    operation: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    provider: Annotated[str | None, StringConstraints(max_length=64)] = None
    model: Annotated[str | None, StringConstraints(max_length=128)] = None
    status: Annotated[str, StringConstraints(pattern=r"^(succeeded|failed|rejected)$")]
    attempt: Annotated[int, Field(ge=1, le=100)]
    input_tokens: Annotated[int | None, Field(ge=0)] = None
    output_tokens: Annotated[int | None, Field(ge=0)] = None
    estimated_cost_usd: Annotated[float | None, Field(ge=0)] = None
    duration_seconds: Annotated[float, Field(ge=0)]
    error_type: Annotated[str | None, StringConstraints(max_length=128)] = None
    error_message: Annotated[str | None, StringConstraints(max_length=512)] = None
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("model call timestamp must include a timezone")
        return value.astimezone(UTC)


class RunUsage(BaseModel):
    """Persisted budget usage restored before retries or checkpoint resumes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_calls: Annotated[int, Field(ge=0)] = 0
    tool_calls: Annotated[int, Field(ge=0)] = 0
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    estimated_cost_usd: Annotated[float, Field(ge=0)] = 0
