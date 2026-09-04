"""Typed contracts for the local-only demo fault boundary."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class FaultName(StrEnum):
    """Allowlisted deterministic demo faults (Stage 09 extends Stage 03)."""

    SLOW_DATABASE = "slow_database"
    POOL_EXHAUSTION = "pool_exhaustion"
    BAD_DEPLOYMENT = "bad_deployment"
    INVENTORY_TIMEOUT = "inventory_timeout"
    CPU_SATURATION = "cpu_saturation"
    HIGH_ERROR_RATE = "high_error_rate"


PAYMENT_FAULTS: tuple[FaultName, ...] = (
    FaultName.SLOW_DATABASE,
    FaultName.POOL_EXHAUSTION,
    FaultName.BAD_DEPLOYMENT,
    FaultName.CPU_SATURATION,
    FaultName.HIGH_ERROR_RATE,
)

INVENTORY_FAULTS: tuple[FaultName, ...] = (FaultName.INVENTORY_TIMEOUT,)


class FaultUpdateRequest(BaseModel):
    """Explicit desired state for one allowlisted fault."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool


class FaultStateResponse(BaseModel):
    """Current state of one allowlisted fault."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: FaultName = FaultName.SLOW_DATABASE
    enabled: bool
    allowed: bool
    delay_seconds: float
    service: str = "payment-service"
    service_version: str
    environment: str


class FaultListResponse(BaseModel):
    """Bounded snapshot of every fault owned by one service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    faults: list[FaultStateResponse]
