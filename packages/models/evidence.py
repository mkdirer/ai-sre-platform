"""Canonical, model-independent evidence and collection contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

EvidenceId = Annotated[str, StringConstraints(pattern=r"^EVD-[A-F0-9]{24}$")]
EvidenceSummary = Annotated[str, StringConstraints(min_length=1, max_length=512)]
TraceId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]


class EvidenceService(StrEnum):
    """Services that fixed local telemetry templates are allowed to select."""

    GATEWAY = "gateway"
    ORDER = "order-service"
    INVENTORY = "inventory-service"
    PAYMENT = "payment-service"
    INCIDENT_API = "incident-api"


class EvidenceSource(StrEnum):
    """Durable source identities used for provenance and bounded labels."""

    PROMETHEUS = "prometheus"
    LOKI = "loki"
    TEMPO = "tempo"
    DEPLOYMENT_STORE = "deployment_store"


class EvidenceType(StrEnum):
    """Canonical Stage 3 evidence categories."""

    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    DEPLOYMENT = "deployment"


class CollectionStatus(StrEnum):
    """Collection outcomes; missing data is never collapsed into a failure."""

    COLLECTED = "collected"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class QueryTemplate(StrEnum):
    """Complete allowlist of deterministic Stage 3 collection operations."""

    METRIC_SERVICE_LATENCY = "metric.service_latency_p95"
    METRIC_SERVICE_ERROR_RATE = "metric.service_error_rate"
    METRIC_SERVICE_CPU = "metric.service_cpu"
    METRIC_SERVICE_MEMORY = "metric.service_memory"
    METRIC_DB_POOL_USAGE = "metric.db_pool_usage"
    LOG_SERVICE_ERRORS = "log.service_errors"
    LOG_GROUPED_PATTERNS = "log.grouped_patterns"
    LOG_AROUND_TIMESTAMP = "log.around_timestamp"
    TRACE_BY_ID = "trace.by_id"
    TRACE_SLOW_SERVICE = "trace.slow_service"
    TRACE_SERVICE_DEPENDENCIES = "trace.service_dependencies"
    DEPLOYMENT_RECENT = "deployment.recent"
    DEPLOYMENT_CURRENT_PREVIOUS = "deployment.current_previous"
    DEPLOYMENT_COMMIT_METADATA = "deployment.commit_metadata"


class EvidenceWindow(BaseModel):
    """An explicit UTC query window; policy-specific bounds are applied by scoping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        """Reject ambiguous datetimes and normalize offsets to UTC."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence window timestamps must include a timezone")
        return value.astimezone(UTC)

    def model_post_init(self, _context: object) -> None:
        if self.end < self.start:
            raise ValueError("evidence window end must not precede start")


class ServiceQuery(BaseModel):
    """Bounded input shared by service telemetry domain methods."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: EvidenceService
    window: EvidenceWindow
    limit: Annotated[int, Field(ge=1, le=100)] = 50


class LogsAroundQuery(ServiceQuery):
    """Safely bounded context query around one incident-owned timestamp."""

    timestamp: datetime
    radius_seconds: Annotated[int, Field(ge=1, le=300)] = 120

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("log context timestamp must include a timezone")
        return value.astimezone(UTC)

    def model_post_init(self, context: object) -> None:
        super().model_post_init(context)
        if not self.window.start <= self.timestamp <= self.window.end:
            raise ValueError("log context timestamp must belong to the evidence window")


class TraceByIdQuery(ServiceQuery):
    """Validated trace lookup without a generic Tempo query parameter."""

    trace_id: TraceId


class EvidenceDraft(BaseModel):
    """Sanitized adapter result before incident ownership and integrity are attached."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: EvidenceSource
    type: EvidenceType
    status: CollectionStatus
    observed_at: datetime
    window: EvidenceWindow
    summary: EvidenceSummary
    payload: dict[str, object] = Field(default_factory=dict)
    query_template: QueryTemplate
    query_parameters: dict[str, object]
    provenance: dict[str, object]
    error_type: Annotated[str | None, StringConstraints(max_length=128)] = None
    error_message: Annotated[str | None, StringConstraints(max_length=512)] = None

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence timestamp must include a timezone")
        return value.astimezone(UTC)

    def model_post_init(self, _context: object) -> None:
        failed = self.status in {
            CollectionStatus.UNAVAILABLE,
            CollectionStatus.FAILED,
            CollectionStatus.TIMED_OUT,
        }
        if failed and (self.error_type is None or self.error_message is None):
            raise ValueError("failed evidence requires a bounded error type and message")
        if not failed and (self.error_type is not None or self.error_message is not None):
            raise ValueError("successful or empty evidence cannot contain an error")

    def with_status(
        self,
        *,
        status: CollectionStatus,
        summary: str,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> Self:
        """Return a status-adjusted copy while retaining deterministic query provenance."""

        return self.model_copy(
            update={
                "status": status,
                "summary": summary,
                "payload": {},
                "error_type": error_type,
                "error_message": error_message,
            }
        )


class EvidenceItem(EvidenceDraft):
    """One durable canonical evidence item exposed through the Incident API."""

    id: EvidenceId
    incident_id: str
    payload_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    collected_at: datetime
    created_at: datetime
    updated_at: datetime

    @field_validator("collected_at", "created_at", "updated_at")
    @classmethod
    def normalize_storage_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence storage timestamps must include a timezone")
        return value.astimezone(UTC)


class EvidencePage(BaseModel):
    """Chronological, incident-isolated evidence page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[EvidenceItem]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0)]


class TimelineEvent(BaseModel):
    """One deterministically ordered event derived from canonical evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(pattern=r"^EVT-[A-F0-9]{24}$")]
    evidence_id: EvidenceId
    incident_id: str
    timestamp: datetime
    source: EvidenceSource
    type: EvidenceType
    status: CollectionStatus
    summary: EvidenceSummary
    attributes: dict[str, object] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def normalize_event_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timeline timestamp must include a timezone")
        return value.astimezone(UTC)


class EvidenceTimelinePage(BaseModel):
    """Stable chronological page of evidence-derived timeline events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[TimelineEvent]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0)]


class SourceCollectionSummary(BaseModel):
    """Per-source persisted collection outcome used by worker audit state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: EvidenceSource
    collected: Annotated[int, Field(ge=0)] = 0
    empty: Annotated[int, Field(ge=0)] = 0
    unavailable: Annotated[int, Field(ge=0)] = 0
    failed: Annotated[int, Field(ge=0)] = 0
    timed_out: Annotated[int, Field(ge=0)] = 0
