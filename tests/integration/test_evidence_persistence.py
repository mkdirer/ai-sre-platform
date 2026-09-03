"""PostgreSQL integration coverage for evidence idempotency and incident isolation."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from packages.config import Settings
from packages.incidents import normalize_webhook
from packages.models.alerts import AlertmanagerWebhook
from packages.models.deployments import DeploymentEnvironment, DeploymentRegistration
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceService,
    EvidenceSource,
    EvidenceType,
    EvidenceWindow,
    QueryTemplate,
)
from packages.persistence import (
    DeploymentConflict,
    SqlAlchemyEvidenceStore,
    SqlAlchemyIncidentStore,
)
from packages.persistence.evidence_rows import EvidenceRow

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _webhook(alert_name: str, minute: int) -> AlertmanagerWebhook:
    started_at = NOW + timedelta(minutes=minute)
    return AlertmanagerWebhook.model_validate(
        {
            "version": "4",
            "status": "firing",
            "receiver": "incident-api",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": alert_name,
                        "service": "payment-service",
                        "severity": "warning",
                    },
                    "annotations": {"summary": alert_name},
                    "startsAt": started_at.isoformat(),
                    "endsAt": "0001-01-01T00:00:00Z",
                }
            ],
        }
    )


def _draft(value: float) -> EvidenceDraft:
    window = EvidenceWindow(start=NOW - timedelta(minutes=10), end=NOW + timedelta(minutes=5))
    return EvidenceDraft(
        source=EvidenceSource.PROMETHEUS,
        type=EvidenceType.METRIC,
        status=CollectionStatus.COLLECTED,
        observed_at=NOW,
        window=window,
        summary=f"Payment latency is {value} seconds",
        payload={"value": value},
        query_template=QueryTemplate.METRIC_SERVICE_LATENCY,
        query_parameters={
            "service": "payment-service",
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "series_limit": 50,
        },
        provenance={"adapter": "prometheus", "api": "v1/query_range"},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evidence_upsert_is_stable_and_incident_isolated(
    migrated_test_database_url: str,
) -> None:
    """Retry updates one stable row and can never make it visible to another incident."""

    engine = create_async_engine(migrated_test_database_url)
    settings = Settings(_env_file=None, environment="test")
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    evidence_store = SqlAlchemyEvidenceStore(settings, engine=engine)
    try:
        first_incident = (
            (await incident_store.ingest(normalize_webhook(_webhook("EvidenceIsolationA", 20))))
            .acceptances[0]
            .incident_id
        )
        second_incident = (
            (await incident_store.ingest(normalize_webhook(_webhook("EvidenceIsolationB", 21))))
            .acceptances[0]
            .incident_id
        )

        first_write = await evidence_store.persist_evidence(
            first_incident,
            [_draft(1.0)],
            collected_at=NOW,
        )
        replay = await evidence_store.persist_evidence(
            first_incident,
            [_draft(2.5)],
            collected_at=NOW + timedelta(seconds=1),
        )
        transient_failure = _draft(0.0).with_status(
            status=CollectionStatus.UNAVAILABLE,
            summary="Prometheus was transiently unavailable",
            error_type="AdapterUnavailableError",
            error_message="Prometheus is unavailable",
        )
        preserved = await evidence_store.persist_evidence(
            first_incident,
            [transient_failure],
            collected_at=NOW + timedelta(seconds=2),
        )
        second_write = await evidence_store.persist_evidence(
            second_incident,
            [_draft(3.0)],
            collected_at=NOW,
        )

        first_page = await evidence_store.list_evidence(first_incident, limit=10, offset=0)
        second_page = await evidence_store.list_evidence(second_incident, limit=10, offset=0)
        async with engine.connect() as connection:
            row_count = await connection.scalar(
                select(func.count(EvidenceRow.id)).where(
                    EvidenceRow.incident_id.in_([first_incident, second_incident])
                )
            )

        assert first_write[0].id == replay[0].id
        assert first_write[0].created_at == replay[0].created_at
        assert replay[0].payload == {"value": 2.5}
        assert first_write[0].payload_sha256 != replay[0].payload_sha256
        assert preserved[0].status == CollectionStatus.COLLECTED
        assert preserved[0].payload == {"value": 2.5}
        assert preserved[0].payload_sha256 == replay[0].payload_sha256
        assert preserved[0].updated_at == replay[0].updated_at
        assert first_write[0].id != second_write[0].id
        assert first_page.total == second_page.total == 1
        assert first_page.items[0].incident_id == first_incident
        assert second_page.items[0].incident_id == second_incident
        assert row_count == 2

        bulk_drafts = []
        for slot in range(101):
            draft = _draft(float(slot))
            if slot == 100:
                draft = draft.model_copy(
                    update={
                        "payload": {
                            "series": [
                                {
                                    "samples": [
                                        {
                                            "timestamp": NOW.isoformat(),
                                            "value": float(slot),
                                            "password": "must-not-survive",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                )
            bulk_drafts.append(
                draft.model_copy(
                    update={
                        "query_parameters": draft.query_parameters | {"sample_slot": slot},
                    }
                )
            )
        await evidence_store.persist_evidence(first_incident, bulk_drafts, collected_at=NOW)
        public_page = await evidence_store.list_evidence(first_incident, limit=100, offset=0)
        correlation_set = await evidence_store.all_evidence(first_incident)

        assert public_page.total == 102
        assert len(public_page.items) == 100
        assert len(correlation_set) == 102
        nested = next(
            item for item in correlation_set if item.query_parameters.get("sample_slot") == 100
        )
        assert nested.payload["series"][0]["samples"][0]["value"] == 100.0  # type: ignore[index]
        assert nested.payload["series"][0]["samples"][0]["password"] == "[REDACTED]"  # type: ignore[index]
        assert [item.id for item in correlation_set] == [
            item.id
            for item in sorted(
                correlation_set,
                key=lambda item: (item.observed_at, item.id),
            )
        ]
    finally:
        await evidence_store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_deployment_registration_is_idempotent_and_read_only_adapter_safe(
    migrated_test_database_url: str,
) -> None:
    """Immutable local metadata supports history/version/commit reads without remote GitHub."""

    engine = create_async_engine(migrated_test_database_url)
    store = SqlAlchemyEvidenceStore(Settings(_env_file=None, environment="test"), engine=engine)
    current = DeploymentRegistration(
        service=EvidenceService.PAYMENT,
        environment=DeploymentEnvironment.TEST,
        version="0.2.0",
        deployed_at=NOW,
        commit_sha="a" * 40,
        changed_files=["apps/demo/payment_service/main.py"],
        metadata={"scenario": "integration", "token": "do-not-store"},
    )
    previous = DeploymentRegistration(
        service=EvidenceService.PAYMENT,
        environment=DeploymentEnvironment.TEST,
        version="0.1.0",
        deployed_at=NOW - timedelta(hours=1),
        commit_sha="b" * 40,
        changed_files=["apps/demo/payment_service/faults.py"],
    )
    earlier_current_version = DeploymentRegistration(
        service=EvidenceService.PAYMENT,
        environment=DeploymentEnvironment.TEST,
        version="0.2.0",
        deployed_at=NOW - timedelta(minutes=30),
        commit_sha="c" * 40,
        changed_files=["apps/demo/payment_service/main.py"],
    )
    try:
        previous_result = await store.register_deployment(previous)
        await store.register_deployment(earlier_current_version)
        first = await store.register_deployment(current)
        replay = await store.register_deployment(current)
        recent = await store.recent_deployments(
            service=EvidenceService.PAYMENT,
            environment=DeploymentEnvironment.TEST,
            start=NOW - timedelta(hours=2),
            end=NOW + timedelta(minutes=1),
            limit=10,
        )
        versions = await store.current_previous_deployments(
            service=EvidenceService.PAYMENT,
            environment=DeploymentEnvironment.TEST,
            at=NOW + timedelta(minutes=1),
        )
        commit = await store.get_deployment(
            deployment_id=first.deployment.id,
            service=EvidenceService.PAYMENT,
        )

        assert previous_result.created is True
        assert first.created is True
        assert replay.created is False
        assert first.deployment.id == replay.deployment.id
        assert "do-not-store" not in str(first.deployment.metadata)
        assert [record.version for record in recent[:3]] == ["0.2.0", "0.2.0", "0.1.0"]
        assert [record.version for record in versions] == ["0.2.0", "0.1.0"]
        assert commit is not None
        assert commit.changed_files == ["apps/demo/payment_service/main.py"]

        conflicting = current.model_copy(update={"metadata": {"scenario": "different"}})
        with pytest.raises(DeploymentConflict):
            await store.register_deployment(conflicting)
    finally:
        await store.close()
