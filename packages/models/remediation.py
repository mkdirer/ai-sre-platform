"""Typed contracts for approval-gated remediation execution (Stage 10).

Recommendation parameters are validated here, separately from execution.
Execution resolves real endpoints deterministically and never accepts a
command or URL from model output.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from packages.models.evidence import EvidenceService
from packages.models.incidents import IncidentId
from packages.models.investigation import RecommendationAction, RecommendationId

RemediationExecutionId = Annotated[str, StringConstraints(pattern=r"^REM-[A-F0-9]{24}$")]
RemediationActionName = Literal["rollback_payment_deployment"]


class RollbackDeploymentParams(BaseModel):
    """Typed parameters for the one executable demo action.

    Shape matches what the investigator emits: the previous deployment
    identity extracted from deployment evidence. The fault scope is not
    model-controlled; execution deterministically disables every
    payment-service fault.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: EvidenceService
    deployment_id: Annotated[str, StringConstraints(pattern=r"^DEP-[A-F0-9]{20}$")]
    version: Annotated[str, StringConstraints(min_length=1, max_length=32)]


class ExecutionStatus(StrEnum):
    """Durable execution lifecycle; terminal states never execute again."""

    PENDING = "pending"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class RemediationExecution(BaseModel):
    """Canonical execution record owned by one approved recommendation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: RemediationExecutionId
    incident_id: IncidentId
    recommendation_id: RecommendationId
    approval_id: Annotated[str, StringConstraints(pattern=r"^APR-[A-F0-9]{24}$")]
    action_type: RecommendationAction
    action_name: RemediationActionName
    target: EvidenceService
    incident_version: Annotated[int, Field(ge=1)]
    status: ExecutionStatus
    attempts: Annotated[int, Field(ge=0)]
    stop_requested: bool = False
    result: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("remediation timestamp must include a timezone")
        return value.astimezone(UTC)


class RemediationExecuteRequest(BaseModel):
    """Human execution input; revalidates the exact approved version/state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_version: Annotated[int, Field(ge=1)]
    expected_service_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    actor: Annotated[str, StringConstraints(min_length=1, max_length=64)]

    @field_validator("actor", mode="before")
    @classmethod
    def _normalize_actor(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class RemediationStopRequest(BaseModel):
    """Manual stop input for a live execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_version: Annotated[int, Field(ge=1)]
    actor: Annotated[str, StringConstraints(min_length=1, max_length=64)]

    @field_validator("actor", mode="before")
    @classmethod
    def _normalize_actor(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ForbiddenRemediationAction(ValueError):
    """Raised when an action is outside the allowlisted execution registry."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RemediationExecutionResponse(BaseModel):
    """Execution outcome; replayed distinguishes idempotent replays from new claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution: RemediationExecution
    replayed: bool


@dataclass(frozen=True)
class RecommendationContext:
    """Approved recommendation inputs backing one execution."""

    action_type: RecommendationAction
    target: EvidenceService
    parameters: dict[str, object]
