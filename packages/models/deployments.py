"""Typed local deployment metadata contracts; no remote SCM integration."""

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)

from packages.models.evidence import EvidenceService, EvidenceWindow

DeploymentId = Annotated[str, StringConstraints(pattern=r"^DEP-[A-F0-9]{20}$")]
DeploymentVersion = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$"),
]
CommitSha = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{7,64}$")]
ChangedFile = Annotated[str, StringConstraints(min_length=1, max_length=256)]
_SAFE_FILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]*$")


class DeploymentEnvironment(StrEnum):
    """Environments accepted by the local metadata registry."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class DeploymentRegistration(BaseModel):
    """Immutable identity and bounded metadata for one deployment event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: EvidenceService
    environment: DeploymentEnvironment
    version: DeploymentVersion
    deployed_at: datetime
    commit_sha: CommitSha
    changed_files: Annotated[list[ChangedFile], Field(max_length=100)] = Field(default_factory=list)
    metadata: Annotated[dict[str, JsonValue], Field(max_length=32)] = Field(default_factory=dict)

    @field_validator("deployed_at")
    @classmethod
    def normalize_deployed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deployment timestamp must include a timezone")
        return value.astimezone(UTC)

    @field_validator("changed_files")
    @classmethod
    def validate_changed_files(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            if (
                value.startswith("/")
                or ".." in value.split("/")
                or _SAFE_FILE_PATTERN.fullmatch(value) is None
            ):
                raise ValueError("changed files must be safe repository-relative paths")
            normalized.append(value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("changed files must not contain duplicates")
        return normalized


class DeploymentRecord(DeploymentRegistration):
    """One persisted local deployment fact."""

    id: DeploymentId
    registered_at: datetime

    @field_validator("registered_at")
    @classmethod
    def normalize_registered_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deployment registration timestamp must include a timezone")
        return value.astimezone(UTC)


class DeploymentRegistrationResponse(BaseModel):
    """Idempotent registration result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment: DeploymentRecord
    created: bool


class DeploymentPage(BaseModel):
    """Newest-first local deployment page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[DeploymentRecord]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0)]


class DeploymentWindowQuery(BaseModel):
    """Typed recent-deployment request for the local read adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: EvidenceService
    environment: DeploymentEnvironment
    window: EvidenceWindow
    limit: Annotated[int, Field(ge=1, le=50)] = 10


class DeploymentAtQuery(BaseModel):
    """Typed current/previous version lookup at an incident-owned time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: EvidenceService
    environment: DeploymentEnvironment
    window: EvidenceWindow
    at: datetime

    @field_validator("at")
    @classmethod
    def normalize_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deployment lookup timestamp must include a timezone")
        return value.astimezone(UTC)


class DeploymentCommitQuery(BaseModel):
    """Typed commit metadata lookup bound to both deployment and service identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_id: DeploymentId
    service: EvidenceService
    window: EvidenceWindow
