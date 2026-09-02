"""Shared HTTP health and error response contracts."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ErrorResponse(BaseModel):
    """Stable error envelope returned by demo services."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    request_id: str


class HealthResponse(BaseModel):
    """Liveness/readiness response with bounded dependency detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str = "ok"
    service: str
    dependencies: dict[str, Annotated[str, Field(max_length=32)]] = Field(default_factory=dict)
