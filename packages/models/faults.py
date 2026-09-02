"""Typed contracts for the local-only demo fault boundary."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class FaultUpdateRequest(BaseModel):
    """Explicit desired state for one allowlisted fault."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool


class FaultStateResponse(BaseModel):
    """Current state of the only Stage 03 fault."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["slow_database"] = "slow_database"
    enabled: bool
    allowed: bool
    delay_seconds: float
    service: Literal["payment-service"] = "payment-service"
    service_version: str
    environment: str
