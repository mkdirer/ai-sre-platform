"""Unit tests for the AI worker service: status mapping, retries, idempotency."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from packages.incidents.worker import AiInvestigationService, WorkerExecutionStatus
from packages.models.incidents import IncidentSeverity
from packages.models.investigation import (
    IncidentReport,
    Recommendation,
    RecommendationAction,
    ReportStatus,
    RootCauseCategory,
)
from packages.persistence import WorkerClaim
from tests.agent.helpers import INCIDENT_ID, JOB_ID, RUN_ID, make_settings

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _report(status: ReportStatus) -> IncidentReport:
    root = RootCauseCategory.DATABASE_LATENCY if status == ReportStatus.COMPLETE else None
    recommendations = []
    if status == ReportStatus.WAITING_FOR_APPROVAL:
        root = RootCauseCategory.BAD_DEPLOYMENT
        recommendations = [
            Recommendation(
                id="REC-AAAAAAAAAAAAAAAAAAAAAAAA",
                action_type=RecommendationAction.ROLLBACK_DEPLOYMENT,
                target="payment-service",
                parameters={"deployment_id": "DEP-0001", "version": "0.1.0"},
                rationale_evidence_ids=[],
                risk="medium",
                reversible=True,
                requires_approval=True,
                status="waiting_for_approval",
            )
        ]
    return IncidentReport(
        id="RPT-AAAAAAAAAAAAAAAAAAAAAAAA",
        incident_id=INCIDENT_ID,
        title="Payment latency",
        affected_services=["payment-service"],
        severity=IncidentSeverity.WARNING,
        summary="fixture report",
        root_cause=root,
        root_cause_summary="database latency affecting payment-service" if root else None,
        confidence=0.8 if root else 0.0,
        timeline=[],
        hypotheses=[],
        evidence_references=[],
        recommendations=recommendations,
        limitations=["fixture gap"] if root is None else [],
        status=status,
        generated_at=NOW,
    )


class _FakeJobStore:
    def __init__(self, *, claimed: bool = True, attempt: int = 1, max_attempts: int = 3) -> None:
        self._claimed = claimed
        self._attempt = attempt
        self._max_attempts = max_attempts
        self.completed: list = []
        self.failures: list = []

    async def claim_job(self, job_id: UUID, incident_id: str) -> WorkerClaim:
        return WorkerClaim(
            claimed=self._claimed,
            reason="claimed" if self._claimed else "terminal",
            job_id=job_id,
            run_id=RUN_ID,
            incident_id=incident_id,
            incident_title="Payment latency",
            service="payment-service",
            affected_services=("payment-service",),
            severity=IncidentSeverity.WARNING,
            started_at=NOW,
            investigation_window_start=NOW,
            investigation_window_end=NOW,
            attempt=self._attempt,
            max_attempts=self._max_attempts,
        )

    async def complete_ai_job(self, job_id: UUID, *, report: IncidentReport) -> None:
        self.completed.append((job_id, report))

    async def record_job_failure(
        self, job_id: UUID, *, error: Exception, retry_delay_seconds: int | None
    ) -> None:
        self.failures.append((job_id, error, retry_delay_seconds))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report_status", "expected"),
    [
        (ReportStatus.COMPLETE, WorkerExecutionStatus.REPORT_GENERATED),
        (ReportStatus.WAITING_FOR_APPROVAL, WorkerExecutionStatus.WAITING_FOR_APPROVAL),
        (ReportStatus.INSUFFICIENT_EVIDENCE, WorkerExecutionStatus.INSUFFICIENT_EVIDENCE),
    ],
)
async def test_ai_service_maps_report_status_to_execution(
    report_status: ReportStatus, expected: WorkerExecutionStatus
) -> None:
    """Each terminal report state has an explicit, observable worker outcome."""

    store = _FakeJobStore()
    service = AiInvestigationService(
        store, make_settings(), operation=lambda _claim: _succeed(_report(report_status))
    )

    result = await service.execute(job_id=JOB_ID, incident_id=INCIDENT_ID)

    assert result.status == expected
    assert result.report is not None
    assert result.report.status == report_status
    assert store.completed


async def _succeed(report: IncidentReport) -> IncidentReport:
    return report


@pytest.mark.asyncio
async def test_ai_service_retries_then_dead_letters() -> None:
    """Operation failures reuse the bounded backoff policy, never retrying forever."""

    async def fail(_claim: WorkerClaim) -> IncidentReport:
        raise RuntimeError("graph failed")

    early = _FakeJobStore(attempt=1)
    early_service = AiInvestigationService(early, make_settings(), operation=fail)
    early_result = await early_service.execute(job_id=JOB_ID, incident_id=INCIDENT_ID)
    assert early_result.status == WorkerExecutionStatus.RETRY_SCHEDULED
    assert early_result.retry_delay_seconds == 2
    assert early_result.error_type == "RuntimeError"

    late = _FakeJobStore(attempt=3)
    late_result = await AiInvestigationService(late, make_settings(), operation=fail).execute(
        job_id=JOB_ID, incident_id=INCIDENT_ID
    )
    assert late_result.status == WorkerExecutionStatus.DEAD_LETTERED
    assert late_result.retry_delay_seconds is None


@pytest.mark.asyncio
async def test_ai_service_skips_unclaimed_jobs() -> None:
    """A lost lease race is a no-op that runs no model work."""

    calls: list = []

    async def fail(_claim: WorkerClaim) -> IncidentReport:
        calls.append(_claim)
        raise AssertionError("must not run")

    store = _FakeJobStore(claimed=False)
    result = await AiInvestigationService(store, make_settings(), operation=fail).execute(
        job_id=JOB_ID, incident_id=INCIDENT_ID
    )

    assert result.status == WorkerExecutionStatus.SKIPPED_IDEMPOTENT
    assert calls == []
    assert store.completed == [] and store.failures == []


def test_no_remediation_surface_exists_on_worker() -> None:
    """The worker module offers no remediation execution entrypoint."""

    import packages.incidents.worker as worker_module

    public = {name for name in dir(worker_module) if not name.startswith("_")}
    assert not any("remediat" in name.lower() or "approv" in name.lower() for name in public)
    assert not hasattr(AiInvestigationService, "execute_remediation")
    assert not hasattr(AiInvestigationService, "approve")
