"""In-process typed contracts for the deterministic Incident API."""

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from apps.incident_api.main import create_app
from packages.config import Settings
from packages.models.deployments import (
    DeploymentPage,
    DeploymentRecord,
    DeploymentRegistration,
    DeploymentRegistrationResponse,
)
from packages.models.evidence import (
    CollectionStatus,
    EvidenceItem,
    EvidencePage,
    EvidenceSource,
    EvidenceTimelinePage,
    EvidenceType,
    EvidenceWindow,
    QueryTemplate,
)
from packages.models.http import ErrorResponse, HealthResponse
from packages.models.incidents import (
    AlertAcceptance,
    AlertIngestResponse,
    AuditEventPage,
    IncidentDetail,
    IncidentPage,
    IncidentSeverity,
    IncidentStatus,
    IncidentSummary,
    InvestigationRunPage,
    QueueJobPage,
    QueueJobStatus,
)
from packages.persistence import IngestBatch, PendingQueueJob
from packages.task_queue import JobPublishError

pytestmark = pytest.mark.contract

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
INCIDENT_ID = "INC-A1B2C3D4E5F60708"
FINGERPRINT = "a" * 64
JOB_ID = UUID("7af2ffbd-50fe-42ae-b8be-58ca28fe3f8e")


def _webhook() -> dict[str, object]:
    return {
        "version": "4",
        "status": "firing",
        "receiver": "incident-api",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "DemoPaymentHighLatency",
                    "service": "payment-service",
                    "severity": "warning",
                },
                "annotations": {"summary": "Payment latency is high"},
                "startsAt": "2026-09-02T12:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus:9090/graph",
                "fingerprint": "source-fingerprint",
            }
        ],
    }


def _summary() -> IncidentSummary:
    return IncidentSummary(
        id=INCIDENT_ID,
        status=IncidentStatus.QUEUED,
        title="Payment latency is high",
        service="payment-service",
        affected_services=["payment-service"],
        severity=IncidentSeverity.WARNING,
        started_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
        alert_fingerprint=FINGERPRINT,
        version=1,
        occurrence_count=1,
    )


def _detail() -> IncidentDetail:
    return IncidentDetail(
        **_summary().model_dump(),
        investigation_window_start=NOW,
        investigation_window_end=NOW,
        root_cause=None,
        confidence=None,
    )


class _FakeStore:
    def __init__(self) -> None:
        self.ingest_calls = 0
        self.published: list[UUID] = []
        self.publish_failures: list[UUID] = []
        self.closed = False

    async def ingest(self, _alerts: tuple[object, ...]) -> IngestBatch:
        self.ingest_calls += 1
        duplicate = self.ingest_calls > 1
        acceptance = AlertAcceptance(
            incident_id=INCIDENT_ID,
            alert_fingerprint=FINGERPRINT,
            incident_status=IncidentStatus.QUEUED,
            occurrence_recorded=not duplicate,
            duplicate=duplicate,
            investigation_enqueued=False,
        )
        jobs = () if duplicate else (PendingQueueJob(id=JOB_ID, incident_id=INCIDENT_ID),)
        return IngestBatch(acceptances=(acceptance,), pending_jobs=jobs)

    async def mark_job_published(self, job_id: UUID) -> None:
        self.published.append(job_id)

    async def mark_job_publish_failed(self, job_id: UUID, _error: Exception) -> None:
        self.publish_failures.append(job_id)

    async def list_incidents(
        self,
        *,
        limit: int,
        offset: int,
        status: IncidentStatus | None = None,
    ) -> IncidentPage:
        assert (limit, offset, status) == (10, 2, IncidentStatus.QUEUED)
        return IncidentPage(items=[_summary()], total=3, limit=limit, offset=offset)

    async def get_incident(self, incident_id: str) -> IncidentDetail | None:
        return _detail() if incident_id == INCIDENT_ID else None

    async def list_timeline(
        self,
        _incident_id: str,
        *,
        limit: int,
        offset: int,
    ) -> AuditEventPage:
        return AuditEventPage(items=[], total=0, limit=limit, offset=offset)

    async def list_runs(
        self,
        _incident_id: str,
        *,
        limit: int,
        offset: int,
    ) -> InvestigationRunPage:
        return InvestigationRunPage(items=[], total=0, limit=limit, offset=offset)

    async def list_jobs(
        self,
        *,
        limit: int,
        offset: int,
        incident_id: str | None = None,
        status: QueueJobStatus | None = None,
    ) -> QueueJobPage:
        return QueueJobPage(items=[], total=0, limit=limit, offset=offset)

    async def is_ready(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


class _FakePublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[UUID, str]] = []

    async def publish(self, *, job_id: UUID, incident_id: str) -> None:
        self.calls.append((job_id, incident_id))
        if self.fail:
            raise JobPublishError("broker unavailable")


class _FakeQueueDependency:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.closed = False

    async def is_ready(self) -> bool:
        return self.ready

    async def close(self) -> None:
        self.closed = True


def _evidence_item() -> EvidenceItem:
    return EvidenceItem(
        id="EVD-A1B2C3D4E5F6070811223344",
        incident_id=INCIDENT_ID,
        source=EvidenceSource.PROMETHEUS,
        type=EvidenceType.METRIC,
        status=CollectionStatus.COLLECTED,
        observed_at=NOW,
        window=EvidenceWindow(start=NOW, end=NOW),
        summary="Payment p95 latency is 2.5 seconds",
        payload={"value": 2.5},
        query_template=QueryTemplate.METRIC_SERVICE_LATENCY,
        query_parameters={"service": "payment-service"},
        provenance={"adapter": "prometheus"},
        payload_sha256="b" * 64,
        collected_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


class _FakeEvidenceStore:
    def __init__(self) -> None:
        self.closed = False
        self.evidence_reads = 0

    async def register_deployment(
        self,
        registration: DeploymentRegistration,
    ) -> DeploymentRegistrationResponse:
        return DeploymentRegistrationResponse(
            deployment=DeploymentRecord(
                **registration.model_dump(),
                id="DEP-A1B2C3D4E5F607081122",
                registered_at=NOW,
            ),
            created=True,
        )

    async def list_deployments(
        self,
        *,
        limit: int,
        offset: int,
        service: object = None,
        environment: object = None,
    ) -> DeploymentPage:
        del service, environment
        return DeploymentPage(items=[], total=0, limit=limit, offset=offset)

    async def list_evidence(
        self,
        _incident_id: str,
        *,
        limit: int,
        offset: int,
        source: object = None,
        status: object = None,
    ) -> EvidencePage:
        del source, status
        self.evidence_reads += 1
        return EvidencePage(items=[_evidence_item()], total=1, limit=limit, offset=offset)

    async def all_evidence(self, _incident_id: str) -> tuple[EvidenceItem, ...]:
        self.evidence_reads += 1
        return (_evidence_item(),)

    async def close(self) -> None:
        self.closed = True


def _client(
    store: _FakeStore,
    publisher: _FakePublisher | None = None,
    queue: _FakeQueueDependency | None = None,
    evidence_store: _FakeEvidenceStore | None = None,
) -> httpx.AsyncClient:
    app = create_app(
        Settings(_env_file=None, environment="test"),
        store=store,  # type: ignore[arg-type]
        publisher=publisher or _FakePublisher(),
        queue_dependency=queue or _FakeQueueDependency(),
        evidence_store=evidence_store or _FakeEvidenceStore(),  # type: ignore[arg-type]
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://incident-api.test",
    )


@pytest.mark.asyncio
async def test_webhook_returns_202_then_deduplicates_without_republishing() -> None:
    """The HTTP contract acknowledges only after durable state and queue publication."""

    store = _FakeStore()
    publisher = _FakePublisher()
    async with _client(store, publisher) as client:
        first = await client.post("/api/v1/alerts", json=_webhook())
        duplicate = await client.post("/api/v1/alerts", json=_webhook())

    first_body = AlertIngestResponse.model_validate(first.json())
    duplicate_body = AlertIngestResponse.model_validate(duplicate.json())
    assert first.status_code == duplicate.status_code == 202
    assert first_body.alerts[0].investigation_enqueued is True
    assert first_body.alerts[0].duplicate is False
    assert duplicate_body.alerts[0].duplicate is True
    assert publisher.calls == [(JOB_ID, INCIDENT_ID)]
    assert store.published == [JOB_ID]
    assert first.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_publish_failure_is_visible_and_preserves_failed_outbox_state() -> None:
    """A broker failure is a typed 503, never a false successful investigation."""

    store = _FakeStore()
    publisher = _FakePublisher(fail=True)
    async with _client(store, publisher) as client:
        response = await client.post("/api/v1/alerts", json=_webhook())

    error = ErrorResponse.model_validate(response.json())
    assert response.status_code == 503
    assert error.code == "queue_unavailable"
    assert store.ingest_calls == 1
    assert store.publish_failures == [JOB_ID]


@pytest.mark.asyncio
async def test_incident_reads_are_paginated_typed_and_not_found_is_stable() -> None:
    """List/detail endpoints enforce bounds, stable models, and a typed 404."""

    store = _FakeStore()
    async with _client(store) as client:
        page_response = await client.get("/api/v1/incidents?limit=10&offset=2&status=queued")
        detail_response = await client.get(f"/api/v1/incidents/{INCIDENT_ID}")
        missing_response = await client.get("/api/v1/incidents/INC-0000000000000000")
        invalid_page = await client.get("/api/v1/incidents?limit=101")

    assert IncidentPage.model_validate(page_response.json()).total == 3
    assert IncidentDetail.model_validate(detail_response.json()).root_cause is None
    assert missing_response.status_code == 404
    assert ErrorResponse.model_validate(missing_response.json()).code == "incident_not_found"
    assert invalid_page.status_code == 422
    assert ErrorResponse.model_validate(invalid_page.json()).code == "validation_error"


@pytest.mark.asyncio
async def test_evidence_timeline_and_deployment_registration_are_typed_and_isolated() -> None:
    """Stage 3 evidence stays incident-owned and deployments use a bounded typed API."""

    store = _FakeStore()
    evidence_store = _FakeEvidenceStore()
    deployment = {
        "service": "payment-service",
        "environment": "test",
        "version": "0.2.0",
        "deployed_at": NOW.isoformat(),
        "commit_sha": "a" * 40,
        "changed_files": ["apps/demo/payment_service/main.py"],
        "metadata": {"scenario": "contract"},
    }
    async with _client(store, evidence_store=evidence_store) as client:
        evidence_response = await client.get(f"/api/v1/incidents/{INCIDENT_ID}/evidence")
        timeline_response = await client.get(f"/api/v1/incidents/{INCIDENT_ID}/evidence/timeline")
        registration_response = await client.post("/api/v1/deployments", json=deployment)
        missing_response = await client.get("/api/v1/incidents/INC-0000000000000000/evidence")

    assert EvidencePage.model_validate(evidence_response.json()).total == 1
    assert EvidenceTimelinePage.model_validate(timeline_response.json()).total == 1
    registered = DeploymentRegistrationResponse.model_validate(registration_response.json())
    assert registration_response.status_code == 201
    assert registered.deployment.commit_sha == "a" * 40
    assert missing_response.status_code == 404
    assert evidence_store.evidence_reads == 2


@pytest.mark.asyncio
async def test_readiness_checks_postgres_and_redis_but_liveness_does_not() -> None:
    """The queue is a direct readiness dependency while liveness remains process-only."""

    store = _FakeStore()
    async with _client(store, queue=_FakeQueueDependency(ready=False)) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

    assert HealthResponse.model_validate(live.json()).service == "incident-api"
    assert live.status_code == 200
    assert ready.status_code == 503
    assert ErrorResponse.model_validate(ready.json()).code == "redis_unavailable"


@pytest.mark.asyncio
async def test_invalid_alert_identity_never_reaches_persistence() -> None:
    """Normalization rejects missing service identity before opening a transaction."""

    store = _FakeStore()
    payload = _webhook()
    alerts = payload["alerts"]
    assert isinstance(alerts, list)
    alert = alerts[0]
    assert isinstance(alert, dict)
    labels = alert["labels"]
    assert isinstance(labels, dict)
    del labels["service"]
    async with _client(store) as client:
        response = await client.post("/api/v1/alerts", json=payload)

    assert response.status_code == 422
    assert ErrorResponse.model_validate(response.json()).code == "invalid_alert"
    assert store.ingest_calls == 0


def test_openapi_documents_current_evidence_endpoints_without_later_stage_apis() -> None:
    """OpenAPI exposes Stage 3 evidence/deployments but no AI or approval APIs."""

    app = create_app(
        Settings(_env_file=None, environment="test"),
        store=_FakeStore(),  # type: ignore[arg-type]
        publisher=_FakePublisher(),
        queue_dependency=_FakeQueueDependency(),
        evidence_store=_FakeEvidenceStore(),  # type: ignore[arg-type]
    )
    schema = app.openapi()
    paths = schema["paths"]

    assert "/api/v1/alerts" in paths
    assert "/api/v1/incidents" in paths
    assert "/api/v1/incidents/{incident_id}/timeline" in paths
    assert "/api/v1/incidents/{incident_id}/evidence" in paths
    assert "/api/v1/incidents/{incident_id}/evidence/timeline" in paths
    assert "/api/v1/deployments" in paths
    assert "/api/v1/incidents/{incident_id}/approve" not in paths
    request_body = paths["/api/v1/alerts"]["post"]["requestBody"]
    assert "firing" in request_body["content"]["application/json"]["examples"]
