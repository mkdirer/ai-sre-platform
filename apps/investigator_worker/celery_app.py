"""Celery entrypoint for the retry-safe Stage 04 no-AI placeholder worker."""

import asyncio
from typing import NoReturn
from uuid import UUID

from celery import Task  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError

from packages.config import Settings
from packages.incidents.worker import (
    PlaceholderInvestigationService,
    WorkerExecution,
    WorkerExecutionStatus,
)
from packages.models.incidents import IncidentId
from packages.persistence import IncidentStoreUnavailable, SqlAlchemyIncidentStore
from packages.task_queue import create_celery_app
from packages.telemetry import TelemetryRuntime, bind_incident_id, reset_incident_id

_incident_id_adapter = TypeAdapter(IncidentId)
celery_app = create_celery_app(Settings(), include_worker=False)


class PlaceholderJobDeadLettered(RuntimeError):
    """Signal a visible Celery failure after the durable retry limit is exhausted."""


@celery_app.task(  # type: ignore[misc]
    bind=True,
    name="incident.process_no_ai_placeholder",
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_no_ai_placeholder(self: Task, incident_id: str) -> dict[str, object]:
    """Process a message whose sole business payload is the canonical incident ID."""

    try:
        validated_incident_id = _incident_id_adapter.validate_python(incident_id)
        task_id = UUID(str(self.request.id))
    except (TypeError, ValueError, ValidationError) as error:
        raise ValueError("invalid incident task identity") from error

    settings = Settings()
    try:
        result = asyncio.run(
            _execute_placeholder_task(
                settings=settings,
                job_id=task_id,
                incident_id=validated_incident_id,
            )
        )
    except IncidentStoreUnavailable as error:
        retry_count = int(self.request.retries)
        if retry_count < settings.investigation_max_attempts - 1:
            raise self.retry(
                exc=RuntimeError("incident persistence unavailable"),
                countdown=settings.investigation_job_lease_seconds,
                max_retries=settings.investigation_max_attempts - 1,
            ) from error
        raise
    if result.status == WorkerExecutionStatus.RETRY_SCHEDULED:
        _retry(self, result)
    if result.status == WorkerExecutionStatus.DEAD_LETTERED:
        raise PlaceholderJobDeadLettered(
            f"placeholder investigation dead-lettered after {result.attempt} attempts"
        )
    return {
        "incident_id": result.incident_id,
        "status": result.status.value,
        "attempt": result.attempt,
    }


def _retry(task: Task, result: WorkerExecution) -> NoReturn:
    delay = result.retry_delay_seconds
    if delay is None:
        raise PlaceholderJobDeadLettered("retry outcome did not contain a retry delay")
    raise task.retry(
        exc=RuntimeError("no-AI placeholder attempt failed"),
        countdown=delay,
        max_retries=max(0, Settings().investigation_max_attempts - 1),
    )


async def _execute_placeholder_task(
    *,
    settings: Settings,
    job_id: UUID,
    incident_id: str,
) -> WorkerExecution:
    telemetry = TelemetryRuntime.create(service_name="investigator-worker", settings=settings)
    store = SqlAlchemyIncidentStore(settings, telemetry=telemetry)
    incident_token = bind_incident_id(incident_id)
    try:
        result = await PlaceholderInvestigationService(store, settings).execute(
            job_id=job_id,
            incident_id=incident_id,
        )
        log_method = (
            telemetry.logger.error
            if result.status == WorkerExecutionStatus.DEAD_LETTERED
            else telemetry.logger.info
        )
        log_method(
            f"investigation.{result.status.value}",
            extra={
                "structured": {
                    "job.id": str(job_id),
                    "investigation.stage": "no_ai_placeholder",
                    "investigation.attempt": result.attempt,
                    "ai.executed": False,
                    "error.type": result.error_type,
                }
            },
        )
        return result
    finally:
        reset_incident_id(incident_token)
        await store.close()
        telemetry.shutdown()
