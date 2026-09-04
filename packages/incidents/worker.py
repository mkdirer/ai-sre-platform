"""Retry-safe deterministic evidence worker service with no AI execution."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from packages.config import Settings
from packages.models.evidence import SourceCollectionSummary
from packages.models.investigation import IncidentReport, ReportStatus
from packages.persistence import WorkerClaim


class WorkerJobStore(Protocol):
    """Persistence boundary required by the evidence worker."""

    async def claim_job(self, job_id: UUID, incident_id: str) -> WorkerClaim: ...

    async def complete_evidence_job(
        self,
        job_id: UUID,
        *,
        source_summaries: list[dict[str, object]],
    ) -> None: ...

    async def complete_ai_job(self, job_id: UUID, *, report: IncidentReport) -> None: ...

    async def record_job_failure(
        self,
        job_id: UUID,
        *,
        error: Exception,
        retry_delay_seconds: int | None,
    ) -> None: ...


class WorkerExecutionStatus(StrEnum):
    """Task-level outcomes used by Celery and deterministic unit tests."""

    EVIDENCE_COLLECTED = "evidence_collected"
    REPORT_GENERATED = "report_generated"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    SKIPPED_IDEMPOTENT = "skipped_idempotent"


@dataclass(frozen=True)
class WorkerExecution:
    """Outcome with source summaries and safe retry classification."""

    status: WorkerExecutionStatus
    incident_id: str
    attempt: int
    source_summaries: tuple[SourceCollectionSummary, ...] = ()
    retry_delay_seconds: int | None = None
    error_type: str | None = None
    report: IncidentReport | None = None


EvidenceOperation = Callable[[WorkerClaim], Awaitable[tuple[SourceCollectionSummary, ...]]]
ReportOperation = Callable[[WorkerClaim], Awaitable[IncidentReport]]


class EvidenceInvestigationService:
    """Claim one job, collect deterministic evidence, and persist retry outcomes."""

    def __init__(
        self,
        store: WorkerJobStore,
        settings: Settings,
        *,
        operation: EvidenceOperation,
    ) -> None:
        self._store = store
        self._settings = settings
        self._operation = operation

    async def execute(self, *, job_id: UUID, incident_id: str) -> WorkerExecution:
        """Execute once; duplicate delivery after completion is an idempotent no-op."""

        claim = await self._store.claim_job(job_id, incident_id)
        if not claim.claimed:
            return WorkerExecution(
                status=WorkerExecutionStatus.SKIPPED_IDEMPOTENT,
                incident_id=incident_id,
                attempt=claim.attempt,
            )
        try:
            summaries = await self._operation(claim)
            await self._store.complete_evidence_job(
                job_id,
                source_summaries=[summary.model_dump(mode="json") for summary in summaries],
            )
        except Exception as error:
            retry_delay = self.retry_delay_seconds(claim.attempt, claim.max_attempts)
            await self._store.record_job_failure(
                job_id,
                error=error,
                retry_delay_seconds=retry_delay,
            )
            return WorkerExecution(
                status=(
                    WorkerExecutionStatus.RETRY_SCHEDULED
                    if retry_delay is not None
                    else WorkerExecutionStatus.DEAD_LETTERED
                ),
                incident_id=incident_id,
                attempt=claim.attempt,
                retry_delay_seconds=retry_delay,
                error_type=type(error).__name__,
            )
        return WorkerExecution(
            status=WorkerExecutionStatus.EVIDENCE_COLLECTED,
            incident_id=incident_id,
            attempt=claim.attempt,
            source_summaries=summaries,
        )

    def retry_delay_seconds(self, attempt: int, max_attempts: int) -> int | None:
        """Return deterministic bounded exponential backoff, or no retry at the limit."""

        if attempt >= max_attempts:
            return None
        delay = self._settings.investigation_retry_base_seconds * (2 ** max(0, attempt - 1))
        return int(min(delay, self._settings.investigation_retry_max_seconds))


class AiInvestigationService:
    """Claim one job, run/resume the graph, and publish only a validated report."""

    def __init__(
        self,
        store: WorkerJobStore,
        settings: Settings,
        *,
        operation: ReportOperation,
    ) -> None:
        self._store = store
        self._settings = settings
        self._operation = operation

    async def execute(self, *, job_id: UUID, incident_id: str) -> WorkerExecution:
        claim = await self._store.claim_job(job_id, incident_id)
        if not claim.claimed:
            return WorkerExecution(
                status=WorkerExecutionStatus.SKIPPED_IDEMPOTENT,
                incident_id=incident_id,
                attempt=claim.attempt,
            )
        try:
            report = await self._operation(claim)
            await self._store.complete_ai_job(job_id, report=report)
        except Exception as error:
            retry_delay = EvidenceInvestigationService(
                self._store,
                self._settings,
                operation=lambda _claim: _empty_summaries(),
            ).retry_delay_seconds(claim.attempt, claim.max_attempts)
            await self._store.record_job_failure(
                job_id,
                error=error,
                retry_delay_seconds=retry_delay,
            )
            return WorkerExecution(
                status=(
                    WorkerExecutionStatus.RETRY_SCHEDULED
                    if retry_delay is not None
                    else WorkerExecutionStatus.DEAD_LETTERED
                ),
                incident_id=incident_id,
                attempt=claim.attempt,
                retry_delay_seconds=retry_delay,
                error_type=type(error).__name__,
            )
        status = {
            ReportStatus.COMPLETE: WorkerExecutionStatus.REPORT_GENERATED,
            ReportStatus.WAITING_FOR_APPROVAL: WorkerExecutionStatus.WAITING_FOR_APPROVAL,
            ReportStatus.INSUFFICIENT_EVIDENCE: WorkerExecutionStatus.INSUFFICIENT_EVIDENCE,
        }[report.status]
        return WorkerExecution(
            status=status,
            incident_id=incident_id,
            attempt=claim.attempt,
            report=report,
        )


async def _empty_summaries() -> tuple[SourceCollectionSummary, ...]:
    return ()
