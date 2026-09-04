"""PostgreSQL migration and durable idempotency integration tests."""

import asyncio
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, func, inspect, select
from sqlalchemy.ext.asyncio import create_async_engine

from packages.config import Settings
from packages.incidents import normalize_webhook
from packages.incidents.worker import EvidenceInvestigationService, WorkerExecutionStatus
from packages.models.alerts import AlertmanagerWebhook
from packages.models.checkout import PaymentRequest
from packages.models.evidence import EvidenceSource, SourceCollectionSummary
from packages.models.incidents import IncidentStatus, QueueJobStatus
from packages.persistence import (
    IdempotencyConflict,
    SqlAlchemyIncidentStore,
    SqlAlchemyPaymentStore,
)
from packages.persistence.incident_rows import (
    AlertOccurrenceRow,
    IncidentRow,
    InvestigationRunRow,
    QueueJobRow,
)
from packages.persistence.payment_store import PaymentRow


def test_migration_graph_has_one_current_head() -> None:
    """Stage 08 extends the single linear migration history."""

    script = ScriptDirectory.from_config(Config("alembic.ini"))

    assert script.get_heads() == ["20260906_0006"]


async def _inspect_schema(database_url: str) -> dict[str, Any]:
    engine = create_async_engine(database_url)

    def inspect_connection(connection: Connection) -> dict[str, Any]:
        inspector = inspect(connection)
        return {
            "tables": inspector.get_table_names(),
            "checks": inspector.get_check_constraints("checkout_payments"),
            "uniques": inspector.get_unique_constraints("checkout_payments"),
        }

    try:
        async with engine.connect() as connection:
            return await connection.run_sync(inspect_connection)
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_migration_applies_to_empty_database_and_is_repeatable(
    migrated_test_database_url: str,
) -> None:
    """The first migration creates the required constraints and can be reapplied at head."""

    schema = asyncio.run(_inspect_schema(migrated_test_database_url))
    unique_names = {constraint["name"] for constraint in schema["uniques"]}
    check_names = {constraint["name"] for constraint in schema["checks"]}

    assert "checkout_payments" in schema["tables"]
    assert unique_names == {
        "uq_checkout_payments_idempotency_key",
        "uq_checkout_payments_order_id",
    }
    assert check_names == {
        "ck_checkout_payments_quantity_positive",
        "ck_checkout_payments_status_confirmed",
        "ck_checkout_payments_total_positive",
        "ck_checkout_payments_unit_price_positive",
    }

    config = Config("alembic.ini")
    config.attributes["database_url"] = migrated_test_database_url
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.integration
def test_incident_migration_upgrades_from_prior_revision(
    migrated_test_database_url: str,
) -> None:
    """A populated Stage 01 revision can move to Stage 3 without auto-create."""

    config = Config("alembic.ini")
    config.attributes["database_url"] = migrated_test_database_url
    try:
        command.downgrade(config, "20260902_0001")
        prior_schema = asyncio.run(_inspect_schema(migrated_test_database_url))
        assert "checkout_payments" in prior_schema["tables"]
        assert "incidents" not in prior_schema["tables"]

        command.upgrade(config, "head")
        upgraded_schema = asyncio.run(_inspect_schema(migrated_test_database_url))
        assert {
            "incidents",
            "alert_occurrences",
            "investigation_runs",
            "queue_jobs",
            "audit_events",
            "evidence",
            "deployments",
        }.issubset(upgraded_schema["tables"])
        command.check(config)
    finally:
        command.upgrade(config, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_payment_store_retry_returns_one_durable_record(
    migrated_test_database_url: str,
) -> None:
    """Exact retries reuse one row while mismatched key reuse is rejected."""

    engine = create_async_engine(migrated_test_database_url)
    store = SqlAlchemyPaymentStore(Settings(_env_file=None), engine=engine)
    payment_request = PaymentRequest(
        order_id=UUID("14af0742-8cf1-44d5-ab4b-a84ff94a8ea8"),
        reservation_id=UUID("1c98b449-76e8-49ca-8e21-2ec33c7c8ac5"),
        customer_id="integration-customer",
        sku="widget-001",
        quantity=2,
        unit_price_cents=1999,
    )

    try:
        first = await store.create_or_get(payment_request, idempotency_key="store-retry-1")
        replay = await store.create_or_get(payment_request, idempotency_key="store-retry-1")

        assert replay.payment_id == first.payment_id
        assert first.idempotent_replay is False
        assert replay.idempotent_replay is True

        with pytest.raises(IdempotencyConflict):
            await store.create_or_get(
                payment_request.model_copy(update={"quantity": 3}),
                idempotency_key="store-retry-1",
            )

        with pytest.raises(IdempotencyConflict):
            await store.create_or_get(payment_request, idempotency_key="different-key-same-order")

        concurrent_request = payment_request.model_copy(
            update={
                "order_id": UUID("c2e4e960-5a17-4dce-ac1c-599891ab8c8c"),
                "reservation_id": UUID("c8999de4-ec4b-46f8-8752-868af339440b"),
            }
        )
        concurrent_results = await asyncio.gather(
            store.create_or_get(concurrent_request, idempotency_key="store-concurrent-1"),
            store.create_or_get(concurrent_request, idempotency_key="store-concurrent-1"),
        )
        assert concurrent_results[0].payment_id == concurrent_results[1].payment_id
        assert sorted(result.idempotent_replay for result in concurrent_results) == [False, True]

        async with engine.connect() as connection:
            retry_rows = (
                await connection.execute(
                    select(PaymentRow).where(PaymentRow.idempotency_key == "store-retry-1")
                )
            ).scalars()
            concurrent_rows = (
                await connection.execute(
                    select(PaymentRow).where(PaymentRow.idempotency_key == "store-concurrent-1")
                )
            ).scalars()
            assert len(list(retry_rows)) == 1
            assert len(list(concurrent_rows)) == 1
    finally:
        await store.close()


def _incident_webhook(*, status: str, ends_at: str) -> AlertmanagerWebhook:
    return AlertmanagerWebhook.model_validate(
        {
            "version": "4",
            "status": status,
            "receiver": "incident-api",
            "alerts": [
                {
                    "status": status,
                    "labels": {
                        "alertname": "IntegrationPaymentLatency",
                        "service": "payment-service",
                        "severity": "warning",
                    },
                    "annotations": {"summary": "Integration payment latency"},
                    "startsAt": "2026-09-02T14:00:00Z",
                    "endsAt": ends_at,
                    "generatorURL": "http://prometheus:9090/graph",
                    "fingerprint": "source-integration-fingerprint",
                }
            ],
        }
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_incident_store_concurrent_delivery_outbox_and_retry_idempotency(
    migrated_test_database_url: str,
) -> None:
    """Concurrent duplicates create one incident/run/job and worker replay is a no-op."""

    settings = Settings(_env_file=None, environment="test")
    engine = create_async_engine(migrated_test_database_url)
    store = SqlAlchemyIncidentStore(settings, engine=engine)
    firing = normalize_webhook(_incident_webhook(status="firing", ends_at="0001-01-01T00:00:00Z"))
    try:
        first, second = await asyncio.gather(store.ingest(firing), store.ingest(firing))

        acceptances = [first.acceptances[0], second.acceptances[0]]
        assert sorted(acceptance.duplicate for acceptance in acceptances) == [False, True]
        pending_jobs = {job.id: job for batch in (first, second) for job in batch.pending_jobs}
        assert len(pending_jobs) == 1
        pending = next(iter(pending_jobs.values()))

        async with engine.connect() as connection:
            incident_count = await connection.scalar(select(func.count(IncidentRow.id)))
            occurrence_count = await connection.scalar(select(func.count(AlertOccurrenceRow.id)))
            run_count = await connection.scalar(select(func.count(InvestigationRunRow.id)))
            job_count = await connection.scalar(select(func.count(QueueJobRow.id)))
            pending_status = await connection.scalar(
                select(QueueJobRow.status).where(QueueJobRow.id == pending.id)
            )
        assert (incident_count, occurrence_count, run_count, job_count) == (1, 1, 1, 1)
        assert pending_status == QueueJobStatus.PENDING_PUBLISH.value

        await store.mark_job_published(pending.id)

        async def collect(_claim: object) -> tuple[SourceCollectionSummary, ...]:
            return (SourceCollectionSummary(source=EvidenceSource.PROMETHEUS, empty=1),)

        worker = EvidenceInvestigationService(store, settings, operation=collect)  # type: ignore[arg-type]
        completed = await worker.execute(job_id=pending.id, incident_id=pending.incident_id)
        replay = await worker.execute(job_id=pending.id, incident_id=pending.incident_id)
        assert completed.status == WorkerExecutionStatus.EVIDENCE_COLLECTED
        assert replay.status == WorkerExecutionStatus.SKIPPED_IDEMPOTENT

        resolved = await store.ingest(
            normalize_webhook(
                _incident_webhook(
                    status="resolved",
                    ends_at="2026-09-02T14:05:00Z",
                )
            )
        )
        assert resolved.pending_jobs == ()
        detail = await store.get_incident(pending.incident_id)
        assert detail is not None
        assert detail.status == IncidentStatus.RESOLVED
        assert detail.occurrence_count == 2

        async with engine.connect() as connection:
            final_job_status = await connection.scalar(
                select(QueueJobRow.status).where(QueueJobRow.id == pending.id)
            )
            final_run_status = await connection.scalar(
                select(InvestigationRunRow.status).where(
                    InvestigationRunRow.id == QueueJobRow.investigation_run_id
                )
            )
        assert final_job_status == QueueJobStatus.COMPLETED.value
        assert final_run_status == "evidence_collected"
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_investigation_failure_is_retried_then_dead_lettered_and_visible(
    migrated_test_database_url: str,
) -> None:
    """Bounded attempts persist retry metadata and a truthful terminal failure state."""

    settings = Settings(
        _env_file=None,
        environment="test",
        investigation_max_attempts=2,
    )
    engine = create_async_engine(migrated_test_database_url)
    store = SqlAlchemyIncidentStore(settings, engine=engine)
    webhook_payload = _incident_webhook(
        status="firing",
        ends_at="0001-01-01T00:00:00Z",
    ).model_dump(by_alias=True, mode="json")
    webhook_payload["alerts"][0]["labels"]["alertname"] = "IntegrationDeadLetter"
    webhook_payload["alerts"][0]["startsAt"] = "2026-09-02T15:00:00Z"
    firing = normalize_webhook(AlertmanagerWebhook.model_validate(webhook_payload))
    try:
        batch = await store.ingest(firing)
        pending = batch.pending_jobs[0]
        await store.mark_job_published(pending.id)

        first_claim = await store.claim_job(pending.id, pending.incident_id)
        assert first_claim.attempt == 1
        await store.record_job_failure(
            pending.id,
            error=RuntimeError("temporary token=do-not-persist"),
            retry_delay_seconds=2,
        )
        retry_page = await store.list_jobs(
            limit=10,
            offset=0,
            incident_id=pending.incident_id,
        )
        assert retry_page.items[0].status == QueueJobStatus.RETRY_SCHEDULED

        second_claim = await store.claim_job(pending.id, pending.incident_id)
        assert second_claim.attempt == 2
        await store.record_job_failure(
            pending.id,
            error=RuntimeError("terminal password=do-not-persist"),
            retry_delay_seconds=None,
        )
        failed_page = await store.list_jobs(
            limit=10,
            offset=0,
            status=QueueJobStatus.DEAD_LETTERED,
        )
        failed_job = next(job for job in failed_page.items if job.id == pending.id)
        detail = await store.get_incident(pending.incident_id)

        assert failed_job.attempts == failed_job.max_attempts == 2
        assert failed_job.last_error_type == "RuntimeError"
        assert "do-not-persist" not in (failed_job.last_error_message or "")
        assert detail is not None
        assert detail.status == IncidentStatus.INVESTIGATION_FAILED
    finally:
        await store.close()
