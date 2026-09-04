"""PostgreSQL integration coverage for approval-gated remediation execution."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from packages.config import Settings
from packages.incidents import normalize_webhook
from packages.models.alerts import AlertmanagerWebhook
from packages.models.deployments import DeploymentEnvironment, DeploymentRegistration
from packages.models.evidence import EvidenceService
from packages.models.incidents import IncidentSeverity, IncidentStatus
from packages.models.investigation import (
    ApprovalDecision,
    Hypothesis,
    HypothesisStatus,
    IncidentReport,
    Recommendation,
    RecommendationAction,
    RecommendationRisk,
    ReportStatus,
    RootCauseCategory,
)
from packages.models.remediation import ExecutionStatus
from packages.persistence import (
    RemediationConflict,
    SqlAlchemyApprovalStore,
    SqlAlchemyEvidenceStore,
    SqlAlchemyIncidentStore,
    SqlAlchemyInvestigationStore,
    SqlAlchemyRemediationStore,
)
from packages.remediation.adapter import AdapterOutcome, AdapterResult
from packages.remediation.service import ExecutionOutcome, RemediationExecutionService

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
FAST = {
    "remediation_verification_window_seconds": 2.0,
    "remediation_verification_poll_seconds": 0.01,
    "remediation_required_healthy_polls": 2,
}


def _webhook(alertname: str, starts_at: str) -> AlertmanagerWebhook:
    return AlertmanagerWebhook.model_validate(
        {
            "version": "4",
            "status": "firing",
            "receiver": "incident-api",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": alertname,
                        "service": "payment-service",
                        "severity": "warning",
                    },
                    "annotations": {"summary": alertname},
                    "startsAt": starts_at,
                    "endsAt": "0001-01-01T00:00:00Z",
                    "generatorURL": "http://prometheus:9090/graph",
                    "fingerprint": f"source-{alertname}",
                }
            ],
        }
    )


def _report(
    incident_id: str,
    index: int,
    previous_id: str,
    good: str,
    *,
    action: RecommendationAction = RecommendationAction.ROLLBACK_DEPLOYMENT,
    parameters: dict[str, object] | None = None,
) -> IncidentReport:
    suffix = f"{index:024X}"
    return IncidentReport(
        id=f"RPT-{suffix}",
        incident_id=incident_id,  # type: ignore[arg-type]
        title="Payment latency is high",
        affected_services=[EvidenceService.PAYMENT],
        severity=IncidentSeverity.WARNING,
        summary="Payment latency regressed after the deploy",
        root_cause=RootCauseCategory.BAD_DEPLOYMENT,
        root_cause_summary="deployment regression affecting payment-service",
        confidence=0.8,
        timeline=[],
        hypotheses=[
            Hypothesis(
                id=f"HYP-{suffix}",
                incident_id=incident_id,  # type: ignore[arg-type]
                category=RootCauseCategory.BAD_DEPLOYMENT,
                description="A recent payment deployment regressed persistence latency",
                status=HypothesisStatus.VERIFIED,
                confidence=0.8,
                supporting_evidence_ids=["EVD-A1B2C3D4E5F6070811223344"],
                contradicting_evidence_ids=[],
                reasoning_summary="Latency coincides with the deployment window",
            )
        ],
        evidence_references=["EVD-A1B2C3D4E5F6070811223344"],
        knowledge_references=[],
        recommendations=[
            Recommendation(
                id=f"REC-{suffix}",
                action_type=action,
                target=EvidenceService.PAYMENT,
                parameters=(
                    parameters
                    if parameters is not None
                    else {
                        "deployment_id": previous_id,
                        "version": good,
                    }
                ),
                rationale_evidence_ids=["EVD-A1B2C3D4E5F6070811223344"],
                risk=RecommendationRisk.MEDIUM,
                reversible=True,
                requires_approval=True,
                status="waiting_for_approval",
            )
        ],
        related_incident_ids=[],
        limitations=[],
        status=ReportStatus.WAITING_FOR_APPROVAL,
        generated_at=NOW,
    )


def _stores(database_url: str, **overrides: object) -> tuple[Settings, object]:
    values: dict[str, object] = {"_env_file": None, "environment": "test"}
    values.update(FAST)
    values.update(overrides)
    settings = Settings(**values)  # type: ignore[arg-type]
    engine = create_async_engine(database_url)
    return settings, engine


async def _seed_deployments(evidence_store: SqlAlchemyEvidenceStore) -> str:
    """Seed the bad/previous pair; return the previous deployment record ID."""

    return await _seed_versioned_deployments(evidence_store, "0.1.0", "0.2.0")


def _versions(index: int) -> tuple[str, str]:
    """Per-test version pair so rollback records never shift other tests."""

    return f"9.{index}.0", f"9.{index}.1"


async def _seed_versioned_deployments(
    evidence_store: SqlAlchemyEvidenceStore, good: str, bad: str
) -> str:
    now = datetime.now(UTC)
    previous_id: str | None = None
    for version, sha, offset in ((good, "b", 60), (bad, "a", 0)):
        response = await evidence_store.register_deployment(
            DeploymentRegistration(
                service=EvidenceService.PAYMENT,
                environment=DeploymentEnvironment.TEST,
                version=version,
                deployed_at=now - timedelta(minutes=offset),
                commit_sha=sha * 40,
                changed_files=["apps/demo/payment_service/main.py"],
            )
        )
        if version == good:
            previous_id = response.deployment.id
    assert previous_id is not None
    return previous_id


async def _approved_rollback(
    incident_store: SqlAlchemyIncidentStore,
    artifact_store: SqlAlchemyInvestigationStore,
    approval_store: SqlAlchemyApprovalStore,
    evidence_store: SqlAlchemyEvidenceStore,
    alertname: str,
    starts_at: str,
    index: int,
) -> tuple[str, str, int, str, str]:
    """Drive alert to approval; expectations derive from registry read-back.

    "Previous" is global registry state (a prior rollback record can outrank
    an older seed), so the report parameters and expected version always come
    from the actual current/previous pair, exactly as production must.
    """

    good, bad = _versions(index)
    await _seed_versioned_deployments(evidence_store, good, bad)
    batch = await incident_store.ingest(normalize_webhook(_webhook(alertname, starts_at)))
    pending = batch.pending_jobs[0]
    await incident_store.mark_job_published(pending.id)
    claim = await incident_store.claim_job(pending.id, pending.incident_id)
    assert claim.claimed
    pair = await evidence_store.current_previous_deployments(
        service=EvidenceService.PAYMENT,
        environment=DeploymentEnvironment.TEST,
        at=datetime.now(UTC),
    )
    assert len(pair) >= 2
    actual_current, actual_previous = pair[0], pair[1]
    report = _report(pending.incident_id, index, actual_previous.id, actual_previous.version)
    runs = await incident_store.list_runs(pending.incident_id, limit=10, offset=0)
    await artifact_store.save_hypotheses(runs.items[0].id, pending.incident_id, report.hypotheses)
    await artifact_store.save_report(runs.items[0].id, report)
    await incident_store.complete_ai_job(pending.id, report=report)
    detail = await incident_store.get_incident(pending.incident_id)
    assert detail is not None
    assert detail.status == IncidentStatus.WAITING_FOR_APPROVAL
    approval = await approval_store.decide(
        report.recommendations[0].id,
        incident_version=detail.version,
        actor="local-demo-approver",
        decision=ApprovalDecision.APPROVED,
        idempotency_key=f"approve-{alertname}",
    )
    assert not approval.replayed
    refreshed = await incident_store.get_incident(pending.incident_id)
    assert refreshed is not None
    return (
        pending.incident_id,
        report.recommendations[0].id,
        refreshed.version,
        actual_previous.version,
        actual_current.version,
    )


class _FakeAdapter:
    def __init__(self, outcomes: list[AdapterResult]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def disable_faults(self, params: object) -> AdapterResult:
        self.calls += 1
        return self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]


def _probe_script(samples: list[float | None]) -> object:
    script = list(samples)

    async def probe(service: object) -> float | None:
        return script.pop(0) if len(script) > 1 else script[0]

    return probe


async def _no_sleep(delay: float) -> None:
    return None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_claim_moves_incident_to_remediating(
    migrated_test_database_url: str,
) -> None:
    """Claiming an approved rollback pauses investigation and starts remediation."""

    settings, engine = _stores(migrated_test_database_url)
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
    approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
    evidence_store = SqlAlchemyEvidenceStore(settings, engine=engine)
    remediation_store = SqlAlchemyRemediationStore(settings, engine=engine)
    try:
        incident_id, rec_id, version, good, bad = await _approved_rollback(
            incident_store,
            artifact_store,
            approval_store,
            evidence_store,
            "RemediationClaim",
            "2026-09-07T12:00:00Z",
            101,
        )
        execution, replayed = await remediation_store.request_execution(
            rec_id,
            incident_version=version,
            expected_service_version=bad,
            actor="local-demo-approver",
            idempotency_key="exec-claim",
        )
        assert not replayed
        assert execution.status == ExecutionStatus.PENDING
        detail = await incident_store.get_incident(incident_id)
        assert detail is not None
        assert detail.status == IncidentStatus.REMEDIATING
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_returns_stored_and_concurrent_loses(
    migrated_test_database_url: str,
) -> None:
    """Same key replays; a racing key conflicts instead of double-executing."""

    settings, engine = _stores(migrated_test_database_url)
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
    approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
    evidence_store = SqlAlchemyEvidenceStore(settings, engine=engine)
    remediation_store = SqlAlchemyRemediationStore(settings, engine=engine)
    try:
        incident_id, rec_id, version, good, bad = await _approved_rollback(
            incident_store,
            artifact_store,
            approval_store,
            evidence_store,
            "RemediationReplay",
            "2026-09-07T12:01:00Z",
            102,
        )

        async def _claim(key: str) -> object:
            return await remediation_store.request_execution(
                rec_id,
                incident_version=version,
                expected_service_version=bad,
                actor="local-demo-approver",
                idempotency_key=key,
            )

        first, replayed = await _claim("exec-replay")
        assert not replayed
        again, was_replay = await _claim("exec-replay")
        assert was_replay
        assert again.id == first.id
        assert incident_id.startswith("INC-")
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_claims_serialize_to_one_execution(
    migrated_test_database_url: str,
) -> None:
    """Racing claims yield one execution; the loser conflicts, never duplicates."""

    settings, engine = _stores(migrated_test_database_url)
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
    approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
    evidence_store = SqlAlchemyEvidenceStore(settings, engine=engine)
    remediation_store = SqlAlchemyRemediationStore(settings, engine=engine)
    try:
        _, rec_id, version, _good, bad = await _approved_rollback(
            incident_store,
            artifact_store,
            approval_store,
            evidence_store,
            "RemediationRace",
            "2026-09-07T12:10:00Z",
            110,
        )

        async def _claim(key: str) -> object:
            return await remediation_store.request_execution(
                rec_id,
                incident_version=version,
                expected_service_version=bad,
                actor="local-demo-approver",
                idempotency_key=key,
            )

        winner, loser = await asyncio.gather(
            _claim("exec-race-a"), _claim("exec-race-b"), return_exceptions=True
        )
        successes = [item for item in (winner, loser) if not isinstance(item, BaseException)]
        conflicts = [item for item in (winner, loser) if isinstance(item, RemediationConflict)]
        assert len(successes) == 1
        assert len(conflicts) == 1
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_version_and_forbidden_action_rejected(
    migrated_test_database_url: str,
) -> None:
    """Stale versions and non-executable actions fail before any mutation."""

    settings, engine = _stores(migrated_test_database_url)
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
    approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
    evidence_store = SqlAlchemyEvidenceStore(settings, engine=engine)
    remediation_store = SqlAlchemyRemediationStore(settings, engine=engine)
    try:
        _, rec_id, version, _good, bad = await _approved_rollback(
            incident_store,
            artifact_store,
            approval_store,
            evidence_store,
            "RemediationStale",
            "2026-09-07T12:02:00Z",
            103,
        )
        with pytest.raises(RemediationConflict) as stale:
            await remediation_store.request_execution(
                rec_id,
                incident_version=version - 1,
                expected_service_version=bad,
                actor="local-demo-approver",
                idempotency_key="exec-stale",
            )
        assert stale.value.code == "stale_version"
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forbidden_action_leaves_incident_paused(
    migrated_test_database_url: str,
) -> None:
    """An approved but non-executable action is rejected without transition."""

    settings, engine = _stores(migrated_test_database_url)
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
    approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
    evidence_store = SqlAlchemyEvidenceStore(settings, engine=engine)
    remediation_store = SqlAlchemyRemediationStore(settings, engine=engine)
    try:
        await _seed_deployments(evidence_store)
        batch = await incident_store.ingest(
            normalize_webhook(_webhook("RemediationForbidden", "2026-09-07T12:03:00Z"))
        )
        pending = batch.pending_jobs[0]
        await incident_store.mark_job_published(pending.id)
        claim = await incident_store.claim_job(pending.id, pending.incident_id)
        assert claim.claimed
        report = _report(
            pending.incident_id,
            104,
            "DEP-AAAAAAAAAAAAAAAAAAAA",
            "0.1.0",
            action=RecommendationAction.INVESTIGATE_DATABASE,
            parameters={},
        )
        runs = await incident_store.list_runs(pending.incident_id, limit=10, offset=0)
        await artifact_store.save_hypotheses(
            runs.items[0].id, pending.incident_id, report.hypotheses
        )
        await artifact_store.save_report(runs.items[0].id, report)
        await incident_store.complete_ai_job(pending.id, report=report)
        detail = await incident_store.get_incident(pending.incident_id)
        assert detail is not None
        await approval_store.decide(
            report.recommendations[0].id,
            incident_version=detail.version,
            actor="local-demo-approver",
            decision=ApprovalDecision.APPROVED,
            idempotency_key="approve-forbidden",
        )
        refreshed = await incident_store.get_incident(pending.incident_id)
        assert refreshed is not None
        with pytest.raises(RemediationConflict) as forbidden:
            await remediation_store.request_execution(
                report.recommendations[0].id,
                incident_version=refreshed.version,
                expected_service_version="0.2.0",
                actor="local-demo-approver",
                idempotency_key="exec-forbidden",
            )
        assert forbidden.value.code == "forbidden_action"
        paused = await incident_store.get_incident(pending.incident_id)
        assert paused is not None
        assert paused.status == IncidentStatus.WAITING_FOR_APPROVAL
    finally:
        await engine.dispose()


def _service(
    remediation_store: SqlAlchemyRemediationStore,
    evidence_store: SqlAlchemyEvidenceStore,
    settings: Settings,
    outcomes: list[AdapterResult],
    samples: list[float | None],
) -> RemediationExecutionService:
    from packages.remediation.service import RemediationExecutionService as Service

    return Service(
        remediation_store,
        remediation_store,
        evidence_store,
        _FakeAdapter(outcomes),
        settings,
        latency_probe=_probe_script(samples),  # type: ignore[arg-type]
        sleeper=_no_sleep,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_path_resolves_incident(migrated_test_database_url: str) -> None:
    """Approval to rollback to verified recovery resolves the incident."""

    settings, engine = _stores(migrated_test_database_url)
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
    approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
    evidence_store = SqlAlchemyEvidenceStore(settings, engine=engine)
    remediation_store = SqlAlchemyRemediationStore(settings, engine=engine)
    try:
        incident_id, rec_id, version, good, bad = await _approved_rollback(
            incident_store,
            artifact_store,
            approval_store,
            evidence_store,
            "RemediationHappy",
            "2026-09-07T12:04:00Z",
            105,
        )
        execution, _ = await remediation_store.request_execution(
            rec_id,
            incident_version=version,
            expected_service_version=bad,
            actor="local-demo-approver",
            idempotency_key="exec-happy",
        )
        service = _service(
            remediation_store,
            evidence_store,
            settings,
            [AdapterResult(AdapterOutcome.APPLIED, detail="disabled")],
            [0.05, 0.04],
        )
        result = await service.execute(execution_id=execution.id, actor="remediation-worker")
        assert result.outcome == ExecutionOutcome.COMPLETED, result.detail
        detail = await incident_store.get_incident(incident_id)
        assert detail is not None
        assert detail.status == IncidentStatus.RESOLVED
        deployments = await evidence_store.current_previous_deployments(
            service=EvidenceService.PAYMENT,
            environment=DeploymentEnvironment.TEST,
            at=datetime.now(UTC),
        )
        assert deployments[0].version == good
        with pytest.raises(RemediationConflict) as completed:
            await remediation_store.request_execution(
                rec_id,
                incident_version=detail.version,
                expected_service_version=good,
                actor="local-demo-approver",
                idempotency_key="exec-happy-again",
            )
        assert completed.value.code == "already_completed"
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_outcome_fails_unresolved(migrated_test_database_url: str) -> None:
    """Unconfirmed adapter outcomes fail the incident without resolving it."""

    settings, engine = _stores(migrated_test_database_url)
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
    approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
    evidence_store = SqlAlchemyEvidenceStore(settings, engine=engine)
    remediation_store = SqlAlchemyRemediationStore(settings, engine=engine)
    try:
        incident_id, rec_id, version, good, bad = await _approved_rollback(
            incident_store,
            artifact_store,
            approval_store,
            evidence_store,
            "RemediationUnknown",
            "2026-09-07T12:05:00Z",
            106,
        )
        execution, _ = await remediation_store.request_execution(
            rec_id,
            incident_version=version,
            expected_service_version=bad,
            actor="local-demo-approver",
            idempotency_key="exec-unknown",
        )
        service = _service(
            remediation_store,
            evidence_store,
            settings,
            [AdapterResult(AdapterOutcome.UNKNOWN, detail="no read-back")],
            [0.05],
        )
        result = await service.execute(execution_id=execution.id, actor="remediation-worker")
        assert result.outcome == ExecutionOutcome.FAILED
        detail = await incident_store.get_incident(incident_id)
        assert detail is not None
        assert detail.status == IncidentStatus.INVESTIGATION_FAILED
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_recovery_and_outage_stay_verifying(migrated_test_database_url: str) -> None:
    """Sustained errors or missing telemetry keep verification open with gaps."""

    for name, alert, index, samples in (
        ("no-recovery", "RemediationNoRecovery", 107, [2.5, 2.6, 2.7, 2.8]),
        ("outage", "RemediationOutage", 108, [None, None, None, None]),
    ):
        settings, engine = _stores(migrated_test_database_url)
        incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
        artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
        approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
        evidence_store = SqlAlchemyEvidenceStore(settings, engine=engine)
        remediation_store = SqlAlchemyRemediationStore(settings, engine=engine)
        try:
            incident_id, rec_id, version, good, bad = await _approved_rollback(
                incident_store,
                artifact_store,
                approval_store,
                evidence_store,
                alert,
                f"2026-09-07T12:0{index - 100}:00Z",
                index,
            )
            execution, _ = await remediation_store.request_execution(
                rec_id,
                incident_version=version,
                expected_service_version=bad,
                actor="local-demo-approver",
                idempotency_key=f"exec-{name}",
            )
            service = _service(
                remediation_store,
                evidence_store,
                settings,
                [AdapterResult(AdapterOutcome.APPLIED, detail="disabled")],
                samples,
            )
            result = await service.execute(execution_id=execution.id, actor="remediation-worker")
            assert result.outcome == ExecutionOutcome.AMBIGUOUS, (name, result.detail)
            detail = await incident_store.get_incident(incident_id)
            assert detail is not None
            assert detail.status == IncidentStatus.VERIFYING, name
        finally:
            await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stop_ends_execution_unresolved(migrated_test_database_url: str) -> None:
    """Stop is synchronous and terminal; late workers find stopped work."""

    settings, engine = _stores(migrated_test_database_url)
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
    approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
    evidence_store = SqlAlchemyEvidenceStore(settings, engine=engine)
    remediation_store = SqlAlchemyRemediationStore(settings, engine=engine)
    try:
        incident_id, rec_id, version, good, bad = await _approved_rollback(
            incident_store,
            artifact_store,
            approval_store,
            evidence_store,
            "RemediationStop",
            "2026-09-07T12:09:00Z",
            109,
        )
        execution, _ = await remediation_store.request_execution(
            rec_id,
            incident_version=version,
            expected_service_version=bad,
            actor="local-demo-approver",
            idempotency_key="exec-stop",
        )
        stopped = await remediation_store.request_stop(
            execution.id, incident_version=version + 1, actor="local-demo-approver"
        )
        assert stopped.stop_requested
        assert stopped.status == ExecutionStatus.STOPPED
        detail = await incident_store.get_incident(incident_id)
        assert detail is not None
        assert detail.status == IncidentStatus.INVESTIGATION_FAILED
        # A repeat stop conflicts instead of duplicating the terminal state.
        with pytest.raises(RemediationConflict) as repeat:
            await remediation_store.request_stop(
                execution.id, incident_version=version + 1, actor="local-demo-approver"
            )
        assert repeat.value.code == "already_completed"
        # A late worker finds stopped work and stands down without writing.
        service = _service(
            remediation_store,
            evidence_store,
            settings,
            [AdapterResult(AdapterOutcome.APPLIED, detail="disabled")],
            [0.05, 0.05],
        )
        result = await service.execute(execution_id=execution.id, actor="remediation-worker")
        assert result.outcome == ExecutionOutcome.SKIPPED_IDEMPOTENT
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reclaim_after_new_cycle_recovers(migrated_test_database_url: str) -> None:
    """A failed execution is reclaimed once a new cycle returns to waiting."""

    settings, engine = _stores(migrated_test_database_url)
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
    approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
    evidence_store = SqlAlchemyEvidenceStore(settings, engine=engine)
    remediation_store = SqlAlchemyRemediationStore(settings, engine=engine)
    try:
        incident_id, rec_id, version, _good, _bad = await _approved_rollback(
            incident_store,
            artifact_store,
            approval_store,
            evidence_store,
            "RemediationReclaim",
            "2026-09-07T12:11:00Z",
            111,
        )
        execution, _ = await remediation_store.request_execution(
            rec_id,
            incident_version=version,
            expected_service_version=_bad,
            actor="local-demo-approver",
            idempotency_key="exec-reclaim-1",
        )
        failing = _service(
            remediation_store,
            evidence_store,
            settings,
            [AdapterResult(AdapterOutcome.FAILED, detail="refused")],
            [0.05],
        )
        failed = await failing.execute(execution_id=execution.id, actor="remediation-worker")
        assert failed.outcome == ExecutionOutcome.FAILED
        # Immediate retry is rejected by design; recovery needs a new cycle.
        current = await incident_store.get_incident(incident_id)
        assert current is not None
        with pytest.raises(RemediationConflict) as early:
            await remediation_store.request_execution(
                rec_id,
                incident_version=current.version,
                expected_service_version=_bad,
                actor="local-demo-approver",
                idempotency_key="exec-reclaim-2",
            )
        assert early.value.code == "invalid_state"
        # A fresh firing reopens the incident; a new investigation waits again.
        batch = await incident_store.ingest(
            normalize_webhook(_webhook("RemediationReclaim", "2026-09-07T13:11:00Z"))
        )
        assert batch.pending_jobs
        reopened = await incident_store.get_incident(incident_id)
        assert reopened is not None
        assert reopened.status == IncidentStatus.QUEUED
        job = batch.pending_jobs[0]
        await incident_store.mark_job_published(job.id)
        claim = await incident_store.claim_job(job.id, incident_id)
        assert claim.claimed
        pair = await evidence_store.current_previous_deployments(
            service=EvidenceService.PAYMENT,
            environment=DeploymentEnvironment.TEST,
            at=datetime.now(UTC),
        )
        assert len(pair) >= 2
        report = _report(incident_id, 112, pair[1].id, pair[1].version)
        runs = await incident_store.list_runs(incident_id, limit=10, offset=0)
        await artifact_store.save_hypotheses(runs.items[0].id, incident_id, report.hypotheses)
        await artifact_store.save_report(runs.items[0].id, report)
        await incident_store.complete_ai_job(job.id, report=report)
        waiting = await incident_store.get_incident(incident_id)
        assert waiting is not None
        assert waiting.status == IncidentStatus.WAITING_FOR_APPROVAL
        reclaimed, replayed = await remediation_store.request_execution(
            rec_id,
            incident_version=waiting.version,
            expected_service_version=pair[0].version,
            actor="local-demo-approver",
            idempotency_key="exec-reclaim-2",
        )
        assert not replayed
        assert reclaimed.id == execution.id
        assert reclaimed.status == ExecutionStatus.PENDING
        recovered = _service(
            remediation_store,
            evidence_store,
            settings,
            [AdapterResult(AdapterOutcome.APPLIED, detail="disabled")],
            [0.05, 0.04],
        )
        result = await recovered.execute(execution_id=reclaimed.id, actor="remediation-worker")
        assert result.outcome == ExecutionOutcome.COMPLETED, result.detail
        resolved = await incident_store.get_incident(incident_id)
        assert resolved is not None
        assert resolved.status == IncidentStatus.RESOLVED
    finally:
        await engine.dispose()
