"""Bounded Alertmanager webhook contracts shared by the stub and Incident API."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

BoundedText = Annotated[str, StringConstraints(max_length=2_048)]
SourceFingerprint = Annotated[str, StringConstraints(max_length=256)]
LabelName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
LabelMap = Annotated[dict[LabelName, BoundedText], Field(max_length=64)]
AlertStatus = Literal["firing", "resolved"]


class AlertmanagerAlert(BaseModel):
    """Relevant fields from one Alertmanager webhook alert."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    status: AlertStatus
    labels: LabelMap
    annotations: LabelMap = Field(default_factory=dict)
    starts_at: datetime = Field(alias="startsAt")
    ends_at: datetime = Field(alias="endsAt")
    generator_url: BoundedText = Field(default="", alias="generatorURL")
    fingerprint: SourceFingerprint = ""


class AlertmanagerWebhook(BaseModel):
    """Validated, size-bounded subset of an Alertmanager webhook delivery."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    version: Literal["4"]
    status: AlertStatus
    receiver: BoundedText
    alerts: Annotated[list[AlertmanagerAlert], Field(min_length=1, max_length=20)]


class AlertDelivery(BaseModel):
    """Deterministic in-memory receipt exposed for scenario verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int
    received_at: datetime
    status: AlertStatus
    receiver: str
    alerts: list[AlertmanagerAlert]


class AlertDeliveryList(BaseModel):
    """Stable ordered view of captured webhook deliveries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deliveries: list[AlertDelivery]


class AlertReceiptResponse(BaseModel):
    """Acknowledgement returned to Alertmanager."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool = True
    sequence: int


class AlertClearResponse(BaseModel):
    """Result of clearing the disposable receiver."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cleared: int
