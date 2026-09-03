"""Typed public contracts for durable alert ingestion and incident inspection."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

IncidentId = Annotated[
    str,
    StringConstraints(pattern=r"^INC-[A-F0-9]{16}$"),
]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
ServiceName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]


class IncidentStatus(StrEnum):
    """Incident lifecycle states defined by the platform domain contract."""

    QUEUED = "queued"
    INVESTIGATING = "investigating"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    REMEDIATING = "remediating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVESTIGATION_FAILED = "investigation_failed"
    REJECTED = "rejected"
    CLOSED = "closed"


class IncidentSeverity(StrEnum):
    """Bounded severity values accepted from normalized alert labels."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class InvestigationRunStatus(StrEnum):
    """Observable states for deterministic pre-AI investigation work."""

    QUEUED = "queued"
    RUNNING = "running"
    PLACEHOLDER_COMPLETE_NO_AI = "placeholder_complete_no_ai"
    EVIDENCE_COLLECTED = "evidence_collected"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"
    SKIPPED_TERMINAL = "skipped_terminal"


class QueueJobStatus(StrEnum):
    """Durable publish, processing, retry, and terminal queue states."""

    PENDING_PUBLISH = "pending_publish"
    QUEUED = "queued"
    PROCESSING = "processing"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    PUBLISH_FAILED = "publish_failed"
    DEAD_LETTERED = "dead_lettered"
    SKIPPED_TERMINAL = "skipped_terminal"


class AlertAcceptance(BaseModel):
    """Per-alert durable ingestion outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: IncidentId
    alert_fingerprint: Fingerprint
    incident_status: IncidentStatus
    occurrence_recorded: bool
    duplicate: bool
    investigation_enqueued: bool


class AlertIngestResponse(BaseModel):
    """Asynchronous acknowledgement returned after durable enqueue."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "accepted": True,
                    "alerts": [
                        {
                            "incident_id": "INC-A1B2C3D4E5F60708",
                            "alert_fingerprint": "a" * 64,
                            "incident_status": "queued",
                            "occurrence_recorded": True,
                            "duplicate": False,
                            "investigation_enqueued": True,
                        }
                    ],
                }
            ]
        },
    )

    accepted: bool = True
    alerts: Annotated[list[AlertAcceptance], Field(min_length=1, max_length=20)]


class IncidentSummary(BaseModel):
    """Stable list representation of one incident."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: IncidentId
    status: IncidentStatus
    title: ShortText
    service: ServiceName
    affected_services: Annotated[list[ServiceName], Field(min_length=1, max_length=32)]
    severity: IncidentSeverity
    started_at: datetime
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    alert_fingerprint: Fingerprint
    version: Annotated[int, Field(ge=1)]
    occurrence_count: Annotated[int, Field(ge=0)]


class IncidentDetail(IncidentSummary):
    """Current canonical incident state; AI report fields remain explicitly empty."""

    investigation_window_start: datetime
    investigation_window_end: datetime
    root_cause: str | None
    confidence: Annotated[float | None, Field(ge=0, le=1)]


class IncidentPage(BaseModel):
    """Offset-based page with stable newest-first ordering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[IncidentSummary]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0)]


class AuditEventResponse(BaseModel):
    """One immutable incident timeline entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    incident_id: IncidentId
    event_type: ShortText
    actor: ShortText
    from_status: IncidentStatus | None
    to_status: IncidentStatus | None
    details: dict[str, object]
    created_at: datetime


class AuditEventPage(BaseModel):
    """Chronological audit timeline page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[AuditEventResponse]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0)]


class InvestigationRunResponse(BaseModel):
    """Observable deterministic investigation attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    incident_id: IncidentId
    stage: Annotated[
        str,
        StringConstraints(pattern=r"^(no_ai_placeholder|evidence_collection)$"),
    ]
    status: InvestigationRunStatus
    attempt: Annotated[int, Field(ge=0)]
    error_type: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class InvestigationRunPage(BaseModel):
    """Newest-first run history for one incident."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[InvestigationRunResponse]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0)]


class QueueJobResponse(BaseModel):
    """Durable queue visibility without exposing message payloads or credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    incident_id: IncidentId
    investigation_run_id: UUID
    status: QueueJobStatus
    attempts: Annotated[int, Field(ge=0)]
    max_attempts: Annotated[int, Field(ge=1)]
    last_error_type: str | None
    last_error_message: str | None
    enqueued_at: datetime | None
    started_at: datetime | None
    next_retry_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class QueueJobPage(BaseModel):
    """Filtered queue-job page used for operational failure visibility."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[QueueJobResponse]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0)]
