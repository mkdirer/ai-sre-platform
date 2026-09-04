"""Celery entrypoint for retry-safe deterministic evidence collection."""

import asyncio
from threading import Thread
from typing import NoReturn
from uuid import UUID
from wsgiref.simple_server import WSGIServer

from celery import Task  # type: ignore[import-untyped]
from celery.signals import (  # type: ignore[import-untyped]
    worker_process_init,
    worker_process_shutdown,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from prometheus_client import start_http_server
from pydantic import TypeAdapter, ValidationError

from packages.agents.evidence_tools import AdditionalEvidenceTools
from packages.agents.provider import BudgetedModelGateway, OpenAIResponsesProvider
from packages.agents.workflow import InvestigatorWorkflow
from packages.config import Settings
from packages.incidents.evidence_collection import EvidenceAdapters, EvidenceCollectionService
from packages.incidents.worker import (
    AiInvestigationService,
    EvidenceInvestigationService,
    WorkerExecution,
    WorkerExecutionStatus,
)
from packages.models.evidence import SourceCollectionSummary
from packages.models.incidents import IncidentId
from packages.models.investigation import IncidentReport
from packages.persistence import (
    IncidentStoreUnavailable,
    SqlAlchemyEvidenceStore,
    SqlAlchemyIncidentStore,
    SqlAlchemyInvestigationStore,
    SqlAlchemyKnowledgeStore,
    WorkerClaim,
)
from packages.persistence.database import build_psycopg_connection_string
from packages.rag.embeddings import build_embedding_provider
from packages.rag.service import KnowledgeService
from packages.task_queue import create_celery_app
from packages.telemetry import TelemetryRuntime, bind_incident_id, reset_incident_id
from packages.tools.deployments import DeploymentAdapter, DeploymentClient
from packages.tools.loki import LokiAdapter, LokiClient
from packages.tools.prometheus import PrometheusAdapter, PrometheusClient
from packages.tools.tempo import TempoAdapter, TempoClient

_incident_id_adapter = TypeAdapter(IncidentId)
celery_app = create_celery_app(Settings(), include_worker=False)
_WORKER_METRICS_PORT = 9464
_worker_telemetry: TelemetryRuntime | None = None
_metrics_server: WSGIServer | None = None
_metrics_thread: Thread | None = None


@worker_process_init.connect(weak=False)  # type: ignore[misc]
def initialize_worker_process(**_kwargs: object) -> None:
    """Create process-owned telemetry and an observable Prometheus endpoint after fork."""

    global _metrics_server, _metrics_thread, _worker_telemetry
    if _worker_telemetry is not None:
        return
    telemetry = TelemetryRuntime.create(service_name="investigator-worker", settings=Settings())
    try:
        server, thread = start_http_server(
            _WORKER_METRICS_PORT,
            addr="0.0.0.0",
            registry=telemetry.metrics.registry,
        )
    except OSError:
        telemetry.shutdown()
        raise
    _worker_telemetry = telemetry
    _metrics_server = server
    _metrics_thread = thread
    telemetry.logger.info(
        "worker.metrics.started",
        extra={"structured": {"server.port": _WORKER_METRICS_PORT}},
    )


@worker_process_shutdown.connect(weak=False)  # type: ignore[misc]
def shutdown_worker_process(**_kwargs: object) -> None:
    """Stop the process-owned metrics server and flush telemetry providers."""

    global _metrics_server, _metrics_thread, _worker_telemetry
    if _metrics_server is not None:
        _metrics_server.shutdown()
        _metrics_server.server_close()
    if _metrics_thread is not None:
        _metrics_thread.join(timeout=2)
    if _worker_telemetry is not None:
        _worker_telemetry.shutdown()
    _metrics_server = None
    _metrics_thread = None
    _worker_telemetry = None


class EvidenceJobDeadLettered(RuntimeError):
    """Signal a visible Celery failure after the durable retry limit is exhausted."""


@celery_app.task(  # type: ignore[misc]
    bind=True,
    name="incident.collect_evidence",
    acks_late=True,
    reject_on_worker_lost=True,
)
def collect_evidence(self: Task, incident_id: str) -> dict[str, object]:
    """Process a message whose sole business payload is the canonical incident ID."""

    return _run_task(self, incident_id)


@celery_app.task(  # type: ignore[misc]
    bind=True,
    name="incident.process_no_ai_placeholder",
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_legacy_placeholder(self: Task, incident_id: str) -> dict[str, object]:
    """Safely upgrade an already-published Stage 04 task to evidence collection."""

    return _run_task(self, incident_id)


def _run_task(task: Task, incident_id: str) -> dict[str, object]:
    try:
        validated_incident_id = _incident_id_adapter.validate_python(incident_id)
        task_id = UUID(str(task.request.id))
    except (TypeError, ValueError, ValidationError) as error:
        raise ValueError("invalid incident task identity") from error

    settings = Settings()
    try:
        result = asyncio.run(
            _execute_evidence_task(
                settings=settings,
                job_id=task_id,
                incident_id=validated_incident_id,
            )
        )
    except IncidentStoreUnavailable as error:
        retry_count = int(task.request.retries)
        if retry_count < settings.investigation_max_attempts - 1:
            raise task.retry(
                exc=RuntimeError("incident persistence unavailable"),
                countdown=settings.investigation_job_lease_seconds,
                max_retries=settings.investigation_max_attempts - 1,
            ) from error
        raise
    if result.status == WorkerExecutionStatus.RETRY_SCHEDULED:
        _retry(task, result)
    if result.status == WorkerExecutionStatus.DEAD_LETTERED:
        raise EvidenceJobDeadLettered(
            f"evidence collection dead-lettered after {result.attempt} attempts"
        )
    return {
        "incident_id": result.incident_id,
        "status": result.status.value,
        "attempt": result.attempt,
        "sources": [summary.model_dump(mode="json") for summary in result.source_summaries],
        "report_id": result.report.id if result.report is not None else None,
        "report_status": result.report.status.value if result.report is not None else None,
        "ai_executed": result.report is not None,
    }


def _retry(task: Task, result: WorkerExecution) -> NoReturn:
    delay = result.retry_delay_seconds
    if delay is None:
        raise EvidenceJobDeadLettered("retry outcome did not contain a retry delay")
    raise task.retry(
        exc=RuntimeError("deterministic evidence collection attempt failed"),
        countdown=delay,
        max_retries=max(0, Settings().investigation_max_attempts - 1),
    )


async def _execute_evidence_task(
    *,
    settings: Settings,
    job_id: UUID,
    incident_id: str,
) -> WorkerExecution:
    owns_telemetry = _worker_telemetry is None
    telemetry = _worker_telemetry or TelemetryRuntime.create(
        service_name="investigator-worker",
        settings=settings,
    )
    incident_store = SqlAlchemyIncidentStore(settings, telemetry=telemetry)
    evidence_store = SqlAlchemyEvidenceStore(settings)
    artifact_store = SqlAlchemyInvestigationStore(settings)
    knowledge_store = SqlAlchemyKnowledgeStore(settings)
    prometheus_client = PrometheusClient(settings, telemetry=telemetry)
    loki_client = LokiClient(settings, telemetry=telemetry)
    tempo_client = TempoClient(settings, telemetry=telemetry)
    collector = EvidenceCollectionService(
        store=evidence_store,
        adapters=EvidenceAdapters(
            prometheus=PrometheusAdapter(prometheus_client),
            loki=LokiAdapter(loki_client),
            tempo=TempoAdapter(tempo_client),
            deployments=DeploymentAdapter(DeploymentClient(evidence_store)),
        ),
        settings=settings,
        telemetry=telemetry,
    )

    async def collect_claim(
        claim: WorkerClaim,
    ) -> tuple[SourceCollectionSummary, ...]:
        return await collector.collect(claim)

    if settings.investigator_enabled:

        async def investigate_claim(claim: WorkerClaim) -> IncidentReport:
            if settings.investigator_provider != "openai":
                raise ValueError("unsupported configured investigator provider")
            provider = OpenAIResponsesProvider(settings)
            try:
                usage = await artifact_store.usage_for_run(claim.run_id)
                gateway = BudgetedModelGateway(
                    provider=provider,
                    store=artifact_store,
                    settings=settings,
                    usage=usage,
                    telemetry=telemetry,
                )
                try:
                    knowledge_provider = build_embedding_provider(settings)
                except Exception as knowledge_error:
                    telemetry.logger.warning(
                        "knowledge.retriever.unavailable",
                        extra={
                            "structured": {
                                "error.type": type(knowledge_error).__name__,
                                "error.message": str(knowledge_error)[:256],
                            }
                        },
                    )
                    knowledge_provider = None
                knowledge_service = (
                    KnowledgeService(
                        settings=settings,
                        store=knowledge_store,
                        provider=knowledge_provider,
                    )
                    if knowledge_provider is not None
                    else None
                )
                async with AsyncPostgresSaver.from_conn_string(
                    build_psycopg_connection_string(settings)
                ) as checkpointer:
                    workflow = InvestigatorWorkflow(
                        settings=settings,
                        checkpointer=checkpointer,
                        evidence_store=evidence_store,
                        artifact_store=artifact_store,
                        collector=collect_claim,
                        model_gateway=gateway,
                        additional_tools=AdditionalEvidenceTools(
                            loki=LokiAdapter(loki_client),
                            tempo=TempoAdapter(tempo_client),
                            evidence_store=evidence_store,
                            call_store=artifact_store,
                            settings=settings,
                            telemetry=telemetry,
                        ),
                        knowledge_retriever=knowledge_service,
                        telemetry=telemetry,
                    )
                    try:
                        return await workflow.run(claim)
                    finally:
                        if knowledge_provider is not None:
                            await knowledge_provider.close()
            finally:
                await provider.close()

        service: AiInvestigationService | EvidenceInvestigationService = AiInvestigationService(
            incident_store,
            settings,
            operation=investigate_claim,
        )
    else:
        service = EvidenceInvestigationService(
            incident_store,
            settings,
            operation=collect_claim,
        )
    incident_token = bind_incident_id(incident_id)
    try:
        result = await service.execute(job_id=job_id, incident_id=incident_id)
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
                    "investigation.stage": "evidence_collection",
                    "investigation.attempt": result.attempt,
                    "evidence.sources": [
                        summary.model_dump(mode="json") for summary in result.source_summaries
                    ],
                    "ai.executed": False,
                    "error.type": result.error_type,
                }
            },
        )
        return result
    finally:
        reset_incident_id(incident_token)
        await asyncio.gather(
            prometheus_client.close(),
            loki_client.close(),
            tempo_client.close(),
            incident_store.close(),
            evidence_store.close(),
            artifact_store.close(),
            knowledge_store.close(),
            return_exceptions=True,
        )
        if owns_telemetry:
            telemetry.shutdown()
