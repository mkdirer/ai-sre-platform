"""Deterministic, bounded incident-to-evidence query scoping."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from packages.config import Settings
from packages.models.evidence import EvidenceService, EvidenceWindow


class IncidentScopeError(ValueError):
    """Canonical incident state cannot be mapped to a safe evidence scope."""


class IncidentScopeInput(Protocol):
    """Incident fields required by scoping, independent of persistence implementation."""

    @property
    def incident_id(self) -> str: ...

    @property
    def incident_title(self) -> str: ...

    @property
    def service(self) -> str: ...

    @property
    def affected_services(self) -> tuple[str, ...]: ...

    @property
    def started_at(self) -> datetime: ...

    @property
    def investigation_window_start(self) -> datetime: ...

    @property
    def investigation_window_end(self) -> datetime: ...


@dataclass(frozen=True)
class IncidentEvidenceScope:
    """Validated source-independent query scope for one incident."""

    incident_id: str
    title: str
    services: tuple[EvidenceService, ...]
    incident_timestamp: datetime
    telemetry_window: EvidenceWindow
    deployment_window: EvidenceWindow


def scope_incident(
    incident: IncidentScopeInput,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> IncidentEvidenceScope:
    """Map canonical state to closed service identities and bounded UTC windows."""

    current_time = _as_utc(now or datetime.now(UTC), "scope evaluation time")
    started_at = _as_utc(incident.started_at, "incident start")
    window = EvidenceWindow(
        start=incident.investigation_window_start,
        end=incident.investigation_window_end,
    )
    if (window.end - window.start).total_seconds() > settings.evidence_max_window_seconds:
        raise IncidentScopeError("incident investigation window exceeds the configured bound")
    earliest = current_time - timedelta(seconds=settings.evidence_max_lookback_seconds)
    latest = current_time + timedelta(seconds=settings.evidence_future_skew_seconds)
    if window.start < earliest:
        raise IncidentScopeError("incident investigation window exceeds maximum lookback")
    if window.end > latest:
        raise IncidentScopeError("incident investigation window exceeds allowed future skew")
    if not window.start <= started_at <= window.end:
        raise IncidentScopeError("incident start is outside its investigation window")

    raw_services = (incident.service, *incident.affected_services)
    services: list[EvidenceService] = []
    for raw_service in raw_services:
        try:
            service = EvidenceService(raw_service)
        except ValueError as error:
            raise IncidentScopeError("incident contains a non-allowlisted service") from error
        if service not in services:
            services.append(service)
    if not services:
        raise IncidentScopeError("incident has no allowlisted service scope")

    return IncidentEvidenceScope(
        incident_id=incident.incident_id,
        title=incident.incident_title,
        services=tuple(services),
        incident_timestamp=started_at,
        telemetry_window=window,
        deployment_window=EvidenceWindow(
            start=window.start - timedelta(seconds=settings.evidence_deployment_lookback_seconds),
            end=window.end,
        ),
    )


def _as_utc(value: datetime, description: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise IncidentScopeError(f"{description} must include a timezone")
    return value.astimezone(UTC)
