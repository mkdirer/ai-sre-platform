"""Durable Alertmanager ingestion and typed Incident API."""

import asyncio
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Body, FastAPI, Query, Request, status

from apps.demo.common.web import (
    ApiError,
    create_service_app,
    get_telemetry,
    register_shutdown_callback,
)
from packages.config import Settings
from packages.incidents import NormalizedAlert, correlate_timeline, normalize_webhook
from packages.models.alerts import AlertmanagerWebhook
from packages.models.deployments import (
    DeploymentEnvironment,
    DeploymentPage,
    DeploymentRegistration,
    DeploymentRegistrationResponse,
)
from packages.models.evidence import (
    CollectionStatus,
    EvidenceItem,
    EvidencePage,
    EvidenceService,
    EvidenceSource,
    EvidenceTimelinePage,
)
from packages.models.http import ErrorResponse, HealthResponse
from packages.models.incidents import (
    AlertIngestResponse,
    AuditEventPage,
    IncidentDetail,
    IncidentId,
    IncidentPage,
    IncidentStatus,
    InvestigationRunPage,
    QueueJobPage,
    QueueJobStatus,
)
from packages.persistence import (
    DeploymentConflict,
    EvidenceStoreUnavailable,
    IncidentStoreUnavailable,
    IngestBatch,
    PendingQueueJob,
    SqlAlchemyEvidenceStore,
    SqlAlchemyIncidentStore,
)
from packages.task_queue import CeleryIncidentPublisher, JobPublishError, RedisDependency
from packages.telemetry import bind_incident_id, reset_incident_id

_WEBHOOK_EXAMPLE = {
    "summary": "Alertmanager firing notification",
    "value": {
        "version": "4",
        "status": "firing",
        "receiver": "incident-api",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "PaymentDatabaseLatencyHigh",
                    "service": "payment-service",
                    "severity": "warning",
                },
                "annotations": {"summary": "Payment database latency is elevated"},
                "startsAt": "2026-09-02T12:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus:9090/graph",
                "fingerprint": "source-owned-fingerprint",
            }
        ],
    },
}


class IncidentStore(Protocol):
    """Operations the HTTP API requires from durable persistence."""

    async def ingest(self, alerts: tuple[NormalizedAlert, ...]) -> IngestBatch: ...

    async def mark_job_published(self, job_id: UUID) -> None: ...

    async def mark_job_publish_failed(self, job_id: UUID, error: Exception) -> None: ...

    async def list_incidents(
        self,
        *,
        limit: int,
        offset: int,
        status: IncidentStatus | None = None,
    ) -> IncidentPage: ...

    async def get_incident(self, incident_id: str) -> IncidentDetail | None: ...

    async def list_timeline(
        self,
        incident_id: str,
        *,
        limit: int,
        offset: int,
    ) -> AuditEventPage: ...

    async def list_runs(
        self,
        incident_id: str,
        *,
        limit: int,
        offset: int,
    ) -> InvestigationRunPage: ...

    async def list_jobs(
        self,
        *,
        limit: int,
        offset: int,
        incident_id: str | None = None,
        status: QueueJobStatus | None = None,
    ) -> QueueJobPage: ...

    async def is_ready(self) -> bool: ...

    async def close(self) -> None: ...


class IncidentJobPublisher(Protocol):
    """Minimal queue boundary that makes transport replaceable in tests."""

    async def publish(self, *, job_id: UUID, incident_id: str) -> None: ...


class EvidenceApiStore(Protocol):
    """Evidence and local deployment operations exposed by the HTTP API."""

    async def register_deployment(
        self,
        registration: DeploymentRegistration,
    ) -> DeploymentRegistrationResponse: ...

    async def list_deployments(
        self,
        *,
        limit: int,
        offset: int,
        service: EvidenceService | None = None,
        environment: DeploymentEnvironment | None = None,
    ) -> DeploymentPage: ...

    async def list_evidence(
        self,
        incident_id: str,
        *,
        limit: int,
        offset: int,
        source: EvidenceSource | None = None,
        status: CollectionStatus | None = None,
    ) -> EvidencePage: ...

    async def all_evidence(self, incident_id: str) -> tuple[EvidenceItem, ...]: ...

    async def close(self) -> None: ...


class QueueReadiness(Protocol):
    """Direct Redis dependency owned by Incident API readiness."""

    async def is_ready(self) -> bool: ...

    async def close(self) -> None: ...


def create_app(
    settings: Settings | None = None,
    *,
    store: IncidentStore | None = None,
    publisher: IncidentJobPublisher | None = None,
    queue_dependency: QueueReadiness | None = None,
    evidence_store: EvidenceApiStore | None = None,
) -> FastAPI:
    """Build the Incident API with injectable persistence and queue boundaries."""

    resolved_settings = settings or Settings()
    app = create_service_app(
        title="AI SRE Incident API",
        service_name="incident-api",
        settings=resolved_settings,
    )
    telemetry = get_telemetry(app)
    resolved_store: IncidentStore = store or SqlAlchemyIncidentStore(
        resolved_settings,
        telemetry=telemetry,
    )
    resolved_publisher = publisher or CeleryIncidentPublisher(resolved_settings)
    resolved_queue: QueueReadiness = queue_dependency or RedisDependency(resolved_settings)
    resolved_evidence_store: EvidenceApiStore = evidence_store or SqlAlchemyEvidenceStore(
        resolved_settings
    )
    register_shutdown_callback(app, resolved_store.close)
    register_shutdown_callback(app, resolved_queue.close)
    register_shutdown_callback(app, resolved_evidence_store.close)

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(service="incident-api")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
    )
    async def readiness() -> HealthResponse:
        postgres_ready, redis_ready = await asyncio.gather(
            resolved_store.is_ready(),
            resolved_queue.is_ready(),
        )
        if not postgres_ready:
            raise ApiError(503, "postgres_unavailable", "PostgreSQL is not ready")
        if not redis_ready:
            raise ApiError(503, "redis_unavailable", "Redis is not ready")
        return HealthResponse(
            service="incident-api",
            dependencies={"postgres": "ready", "redis": "ready"},
        )

    @app.post(
        "/api/v1/alerts",
        response_model=AlertIngestResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses={422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def ingest_alerts(
        webhook: Annotated[
            AlertmanagerWebhook,
            Body(openapi_examples={"firing": _WEBHOOK_EXAMPLE}),
        ],
        _request: Request,
    ) -> AlertIngestResponse:
        try:
            normalized = normalize_webhook(webhook)
        except ValueError as error:
            raise ApiError(422, "invalid_alert", str(error)) from error
        try:
            batch = await resolved_store.ingest(normalized)
            published_jobs = await _publish_jobs(
                batch,
                store=resolved_store,
                publisher=resolved_publisher,
            )
        except IncidentStoreUnavailable as error:
            raise ApiError(
                503, "persistence_unavailable", "incident persistence is unavailable"
            ) from error
        except JobPublishError as error:
            raise ApiError(503, "queue_unavailable", "incident queue is unavailable") from error

        published_incidents = {job.incident_id for job in published_jobs}
        response_alerts = tuple(
            acceptance.model_copy(
                update={
                    "investigation_enqueued": acceptance.incident_id in published_incidents,
                }
            )
            for acceptance in batch.acceptances
        )
        for acceptance in response_alerts:
            token = bind_incident_id(acceptance.incident_id)
            try:
                telemetry.logger.info(
                    "alertmanager.webhook.persisted",
                    extra={
                        "structured": {
                            "alert.fingerprint": acceptance.alert_fingerprint,
                            "alert.duplicate": acceptance.duplicate,
                            "investigation.enqueued": acceptance.investigation_enqueued,
                            "incident.status": acceptance.incident_status.value,
                        }
                    },
                )
            finally:
                reset_incident_id(token)
        return AlertIngestResponse(alerts=list(response_alerts))

    @app.get(
        "/api/v1/incidents",
        response_model=IncidentPage,
        responses={503: {"model": ErrorResponse}},
    )
    async def list_incidents(
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        incident_status: Annotated[IncidentStatus | None, Query(alias="status")] = None,
    ) -> IncidentPage:
        try:
            return await resolved_store.list_incidents(
                limit=limit,
                offset=offset,
                status=incident_status,
            )
        except IncidentStoreUnavailable as error:
            raise ApiError(
                503, "persistence_unavailable", "incident persistence is unavailable"
            ) from error

    @app.get(
        "/api/v1/incidents/{incident_id}",
        response_model=IncidentDetail,
        responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def get_incident(incident_id: IncidentId) -> IncidentDetail:
        return await _require_incident(resolved_store, incident_id)

    @app.get(
        "/api/v1/incidents/{incident_id}/timeline",
        response_model=AuditEventPage,
        responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def get_timeline(
        incident_id: IncidentId,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    ) -> AuditEventPage:
        await _require_incident(resolved_store, incident_id)
        try:
            return await resolved_store.list_timeline(incident_id, limit=limit, offset=offset)
        except IncidentStoreUnavailable as error:
            raise ApiError(
                503, "persistence_unavailable", "incident persistence is unavailable"
            ) from error

    @app.get(
        "/api/v1/incidents/{incident_id}/investigation-runs",
        response_model=InvestigationRunPage,
        responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def get_investigation_runs(
        incident_id: IncidentId,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    ) -> InvestigationRunPage:
        await _require_incident(resolved_store, incident_id)
        try:
            return await resolved_store.list_runs(incident_id, limit=limit, offset=offset)
        except IncidentStoreUnavailable as error:
            raise ApiError(
                503, "persistence_unavailable", "incident persistence is unavailable"
            ) from error

    @app.get(
        "/api/v1/incidents/{incident_id}/evidence",
        response_model=EvidencePage,
        responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def get_evidence(
        incident_id: IncidentId,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        source: Annotated[EvidenceSource | None, Query()] = None,
        collection_status: Annotated[
            CollectionStatus | None,
            Query(alias="status"),
        ] = None,
    ) -> EvidencePage:
        await _require_incident(resolved_store, incident_id)
        try:
            return await resolved_evidence_store.list_evidence(
                incident_id,
                limit=limit,
                offset=offset,
                source=source,
                status=collection_status,
            )
        except EvidenceStoreUnavailable as error:
            raise ApiError(
                503, "persistence_unavailable", "evidence persistence is unavailable"
            ) from error

    @app.get(
        "/api/v1/incidents/{incident_id}/evidence/timeline",
        response_model=EvidenceTimelinePage,
        responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def get_evidence_timeline(
        incident_id: IncidentId,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    ) -> EvidenceTimelinePage:
        await _require_incident(resolved_store, incident_id)
        try:
            evidence = await resolved_evidence_store.all_evidence(incident_id)
        except EvidenceStoreUnavailable as error:
            raise ApiError(
                503, "persistence_unavailable", "evidence persistence is unavailable"
            ) from error
        return correlate_timeline(evidence, limit=limit, offset=offset)

    @app.post(
        "/api/v1/deployments",
        response_model=DeploymentRegistrationResponse,
        status_code=status.HTTP_201_CREATED,
        responses={409: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def register_deployment(
        registration: DeploymentRegistration,
    ) -> DeploymentRegistrationResponse:
        try:
            return await resolved_evidence_store.register_deployment(registration)
        except DeploymentConflict as error:
            raise ApiError(409, "deployment_conflict", str(error)) from error
        except EvidenceStoreUnavailable as error:
            raise ApiError(
                503, "persistence_unavailable", "deployment persistence is unavailable"
            ) from error

    @app.get(
        "/api/v1/deployments",
        response_model=DeploymentPage,
        responses={503: {"model": ErrorResponse}},
    )
    async def list_deployments(
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        service: Annotated[EvidenceService | None, Query()] = None,
        environment: Annotated[DeploymentEnvironment | None, Query()] = None,
    ) -> DeploymentPage:
        try:
            return await resolved_evidence_store.list_deployments(
                limit=limit,
                offset=offset,
                service=service,
                environment=environment,
            )
        except EvidenceStoreUnavailable as error:
            raise ApiError(
                503, "persistence_unavailable", "deployment persistence is unavailable"
            ) from error

    @app.get(
        "/api/v1/investigation-jobs",
        response_model=QueueJobPage,
        responses={503: {"model": ErrorResponse}},
    )
    async def list_investigation_jobs(
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        incident_id: Annotated[IncidentId | None, Query()] = None,
        job_status: Annotated[QueueJobStatus | None, Query(alias="status")] = None,
    ) -> QueueJobPage:
        try:
            return await resolved_store.list_jobs(
                limit=limit,
                offset=offset,
                incident_id=incident_id,
                status=job_status,
            )
        except IncidentStoreUnavailable as error:
            raise ApiError(
                503, "persistence_unavailable", "incident persistence is unavailable"
            ) from error

    return app


async def _publish_jobs(
    batch: IngestBatch,
    *,
    store: IncidentStore,
    publisher: IncidentJobPublisher,
) -> tuple[PendingQueueJob, ...]:
    published: list[PendingQueueJob] = []
    for job in batch.pending_jobs:
        try:
            await publisher.publish(job_id=job.id, incident_id=job.incident_id)
            await store.mark_job_published(job.id)
        except JobPublishError as error:
            await store.mark_job_publish_failed(job.id, error)
            raise
        published.append(job)
    return tuple(published)


async def _require_incident(store: IncidentStore, incident_id: str) -> IncidentDetail:
    try:
        incident = await store.get_incident(incident_id)
    except IncidentStoreUnavailable as error:
        raise ApiError(
            503, "persistence_unavailable", "incident persistence is unavailable"
        ) from error
    if incident is None:
        raise ApiError(404, "incident_not_found", "Incident was not found")
    return incident
