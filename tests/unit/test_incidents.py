"""Unit tests for alert normalization, lifecycle policy, queue payloads, and worker retries."""

import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from packages.config import Settings
from packages.incidents import (
    InvalidStatusTransition,
    StatusTransitionService,
    alert_fingerprint,
    normalize_webhook,
)
from packages.incidents.worker import (
    EvidenceInvestigationService,
    WorkerExecutionStatus,
)
from packages.models.alerts import AlertmanagerWebhook
from packages.models.evidence import EvidenceSource, SourceCollectionSummary
from packages.models.incidents import IncidentSeverity, IncidentStatus
from packages.persistence import WorkerClaim
from packages.task_queue import CeleryIncidentPublisher
from packages.telemetry import (
    JsonLogFormatter,
    bind_incident_id,
    reset_incident_id,
)

JOB_ID = UUID("7af2ffbd-50fe-42ae-b8be-58ca28fe3f8e")
RUN_ID = UUID("42a9f41a-c334-4ad9-99da-0e52ae33576f")
INCIDENT_ID = "INC-A1B2C3D4E5F60708"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _webhook(
    *, status: str = "firing", ends_at: str = "0001-01-01T00:00:00Z"
) -> AlertmanagerWebhook:
    return AlertmanagerWebhook.model_validate(
        {
            "version": "4",
            "status": status,
            "receiver": "incident-api",
            "alerts": [
                {
                    "status": status,
                    "labels": {
                        "service": "payment-service",
                        "severity": "warning",
                        "alertname": "DemoPaymentHighLatency",
                    },
                    "annotations": {
                        "summary": "Payment latency token=do-not-store",
                    },
                    "startsAt": "2026-09-02T12:00:00Z",
                    "endsAt": ends_at,
                    "generatorURL": "http://prometheus:9090/graph",
                    "fingerprint": "untrusted-source-value",
                }
            ],
        }
    )


def test_alert_fingerprint_and_delivery_normalization_are_stable() -> None:
    """Label order and source fingerprint do not affect repository-owned identity."""

    left = alert_fingerprint({"service": "payment-service", "alertname": "Latency"})
    right = alert_fingerprint({"alertname": "Latency", "service": "payment-service"})
    firing = normalize_webhook(_webhook())[0]
    resolved = normalize_webhook(_webhook(status="resolved", ends_at="2026-09-02T12:05:00Z"))[0]

    assert left == right
    assert firing.alert_fingerprint == resolved.alert_fingerprint
    assert firing.delivery_fingerprint != resolved.delivery_fingerprint
    assert firing.ends_at is None
    assert resolved.ends_at is not None
    assert firing.source_fingerprint == "untrusted-source-value"
    assert "do-not-store" not in firing.title


def test_normalization_requires_repository_identity_labels() -> None:
    """Alerts without a service cannot create an ambiguous durable incident."""

    payload = _webhook().model_dump(by_alias=True, mode="json")
    del payload["alerts"][0]["labels"]["service"]

    with pytest.raises(ValueError, match="service"):
        normalize_webhook(AlertmanagerWebhook.model_validate(payload))


def test_source_fingerprint_is_bounded_and_secret_safe() -> None:
    """Untrusted provenance fits its column and cannot retain obvious credentials."""

    payload = _webhook().model_dump(by_alias=True, mode="json")
    payload["alerts"][0]["fingerprint"] = "token=do-not-store"

    normalized = normalize_webhook(AlertmanagerWebhook.model_validate(payload))[0]

    assert normalized.source_fingerprint == "token=[REDACTED]"

    payload["alerts"][0]["fingerprint"] = ("token=x " * 32)[:256]
    expanded_redaction = normalize_webhook(AlertmanagerWebhook.model_validate(payload))[0]
    assert expanded_redaction.source_fingerprint is not None
    assert len(expanded_redaction.source_fingerprint) == 256

    payload["alerts"][0]["fingerprint"] = "x" * 257
    with pytest.raises(ValueError, match="256 characters"):
        AlertmanagerWebhook.model_validate(payload)


def test_status_transition_service_rejects_invalid_movement() -> None:
    """Lifecycle changes go through the explicit allowlist and remain idempotent."""

    service = StatusTransitionService()

    assert (
        service.transition(IncidentStatus.QUEUED, IncidentStatus.INVESTIGATING)
        == IncidentStatus.INVESTIGATING
    )
    assert (
        service.transition(IncidentStatus.INVESTIGATING, IncidentStatus.INVESTIGATING)
        == IncidentStatus.INVESTIGATING
    )
    with pytest.raises(InvalidStatusTransition):
        service.transition(IncidentStatus.QUEUED, IncidentStatus.REMEDIATING)


class _FakeCelery:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_task(self, name: str, **options: object) -> object:
        self.calls.append({"name": name, **options})
        return object()


@pytest.mark.asyncio
async def test_queue_message_contains_only_incident_id() -> None:
    """Full webhook data never crosses the Celery business-payload boundary."""

    fake = _FakeCelery()
    publisher = CeleryIncidentPublisher(
        Settings(_env_file=None),
        celery_app=fake,  # type: ignore[arg-type]
    )

    await publisher.publish(job_id=JOB_ID, incident_id=INCIDENT_ID)

    assert fake.calls == [
        {
            "name": "incident.collect_evidence",
            "args": [INCIDENT_ID],
            "task_id": str(JOB_ID),
            "queue": "incidents",
        }
    ]
    assert "labels" not in json.dumps(fake.calls)


class _WorkerStore:
    def __init__(self, *, attempt: int = 1, claim_once: bool = True) -> None:
        self.attempt = attempt
        self.claim_once = claim_once
        self.claim_calls = 0
        self.completed = 0
        self.failures: list[int | None] = []

    async def claim_job(self, job_id: UUID, incident_id: str) -> WorkerClaim:
        self.claim_calls += 1
        claimed = not self.claim_once or self.claim_calls == 1
        return WorkerClaim(
            claimed=claimed,
            reason="claimed" if claimed else "terminal",
            job_id=job_id,
            run_id=RUN_ID,
            incident_id=incident_id,
            incident_title="Payment latency",
            service="payment-service",
            affected_services=("payment-service",),
            severity=IncidentSeverity.WARNING,
            started_at=NOW,
            investigation_window_start=NOW - timedelta(minutes=10),
            investigation_window_end=NOW + timedelta(minutes=5),
            attempt=self.attempt,
            max_attempts=3,
        )

    async def complete_evidence_job(
        self,
        _job_id: UUID,
        *,
        source_summaries: list[dict[str, object]],
    ) -> None:
        assert source_summaries
        self.completed += 1

    async def record_job_failure(
        self,
        _job_id: UUID,
        *,
        error: Exception,
        retry_delay_seconds: int | None,
    ) -> None:
        assert isinstance(error, RuntimeError)
        self.failures.append(retry_delay_seconds)


@pytest.mark.asyncio
async def test_evidence_worker_is_retry_idempotent_and_explicitly_no_ai() -> None:
    """A duplicate job is a no-op after deterministic evidence collection completes."""

    store = _WorkerStore()

    async def collect(_claim: WorkerClaim) -> tuple[SourceCollectionSummary, ...]:
        return (SourceCollectionSummary(source=EvidenceSource.PROMETHEUS, collected=1),)

    service = EvidenceInvestigationService(
        store,
        Settings(_env_file=None),
        operation=collect,
    )

    first = await service.execute(job_id=JOB_ID, incident_id=INCIDENT_ID)
    replay = await service.execute(job_id=JOB_ID, incident_id=INCIDENT_ID)

    assert first.status == WorkerExecutionStatus.EVIDENCE_COLLECTED
    assert replay.status == WorkerExecutionStatus.SKIPPED_IDEMPOTENT
    assert store.completed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempt", "expected_status", "expected_delay"),
    [
        (1, WorkerExecutionStatus.RETRY_SCHEDULED, 2),
        (3, WorkerExecutionStatus.DEAD_LETTERED, None),
    ],
)
async def test_evidence_worker_records_retry_or_dead_letter(
    attempt: int,
    expected_status: WorkerExecutionStatus,
    expected_delay: int | None,
) -> None:
    """Failure is never reported as a successful investigation."""

    async def fail(_claim: WorkerClaim) -> tuple[SourceCollectionSummary, ...]:
        raise RuntimeError("deterministic failure token=secret")

    store = _WorkerStore(attempt=attempt, claim_once=False)
    service = EvidenceInvestigationService(
        store,
        Settings(_env_file=None),
        operation=fail,
    )

    result = await service.execute(job_id=JOB_ID, incident_id=INCIDENT_ID)

    assert result.status == expected_status
    assert result.retry_delay_seconds == expected_delay
    assert store.failures == [expected_delay]
    assert store.completed == 0


def test_structured_log_includes_incident_context() -> None:
    """Incident correlation is a first-class safe JSON log field."""

    formatter = JsonLogFormatter(
        service_name="incident-api",
        service_version="0.1.0",
        environment="test",
    )
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="incident.persisted",
        args=(),
        exc_info=None,
    )
    token = bind_incident_id(INCIDENT_ID)
    try:
        payload = json.loads(formatter.format(record))
    finally:
        reset_incident_id(token)

    assert payload["incident_id"] == INCIDENT_ID
