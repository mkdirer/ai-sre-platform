"""Retry-safe, explicitly no-AI Stage 04 investigation worker service."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from packages.config import Settings
from packages.persistence import WorkerClaim


class WorkerJobStore(Protocol):
    """Persistence boundary required by the placeholder worker."""

    async def claim_job(self, job_id: UUID, incident_id: str) -> WorkerClaim: ...

    async def complete_placeholder_job(self, job_id: UUID) -> None: ...

    async def record_job_failure(
        self,
        job_id: UUID,
        *,
        error: Exception,
        retry_delay_seconds: int | None,
    ) -> None: ...


class WorkerExecutionStatus(StrEnum):
    """Task-level outcomes used by Celery and deterministic unit tests."""

    PLACEHOLDER_COMPLETE_NO_AI = "placeholder_complete_no_ai"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    SKIPPED_IDEMPOTENT = "skipped_idempotent"


@dataclass(frozen=True)
class WorkerExecution:
    """Outcome with optional retry countdown and safe error classification."""

    status: WorkerExecutionStatus
    incident_id: str
    attempt: int
    retry_delay_seconds: int | None = None
    error_type: str | None = None


PlaceholderOperation = Callable[[WorkerClaim], Awaitable[None]]


class PlaceholderInvestigationService:
    """Load canonical state, mark a no-AI checkpoint, and persist every retry outcome."""

    def __init__(
        self,
        store: WorkerJobStore,
        settings: Settings,
        *,
        operation: PlaceholderOperation | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._operation = operation or _no_ai_placeholder

    async def execute(self, *, job_id: UUID, incident_id: str) -> WorkerExecution:
        """Execute once; duplicate delivery is a successful idempotent no-op."""

        claim = await self._store.claim_job(job_id, incident_id)
        if not claim.claimed:
            return WorkerExecution(
                status=WorkerExecutionStatus.SKIPPED_IDEMPOTENT,
                incident_id=incident_id,
                attempt=claim.attempt,
            )
        try:
            await self._operation(claim)
            await self._store.complete_placeholder_job(job_id)
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
            status=WorkerExecutionStatus.PLACEHOLDER_COMPLETE_NO_AI,
            incident_id=incident_id,
            attempt=claim.attempt,
        )

    def retry_delay_seconds(self, attempt: int, max_attempts: int) -> int | None:
        """Return deterministic bounded exponential backoff, or no retry at the limit."""

        if attempt >= max_attempts:
            return None
        delay = self._settings.investigation_retry_base_seconds * (2 ** max(0, attempt - 1))
        return int(min(delay, self._settings.investigation_retry_max_seconds))


async def _no_ai_placeholder(_claim: WorkerClaim) -> None:
    """Deliberately perform no AI/evidence work during Stage 04."""
