"""PostgreSQL integration coverage for human approval decisions."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from packages.config import Settings
from packages.incidents import normalize_webhook
from packages.models.alerts import AlertmanagerWebhook
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
from packages.persistence import (
    ApprovalConflict,
    SqlAlchemyApprovalStore,
    SqlAlchemyIncidentStore,
    SqlAlchemyInvestigationStore,
)
from packages.persistence.approval_rows import ApprovalRow
from packages.persistence.incident_rows import AuditEventRow

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


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


def _report(incident_id: str, index: int) -> IncidentReport:
    suffix = f"{index:024X}"
    return IncidentReport(
        id=f"RPT-{suffix}",
        incident_id=incident_id,  # type: ignore[arg-type]
        title="Payment latency is high",
        affected_services=[EvidenceService.PAYMENT],
        severity=IncidentSeverity.WARNING,
        summary="Payment latency is high: deployment regression affecting payment-service",
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
                action_type=RecommendationAction.ROLLBACK_DEPLOYMENT,
                target=EvidenceService.PAYMENT,
                parameters={"deployment_id": "DEP-A1B2C3D4E5F607081122", "version": "0.1.0"},
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


async def _waiting_incident(
    incident_store: SqlAlchemyIncidentStore,
    artifact_store: SqlAlchemyInvestigationStore,
    alertname: str,
    starts_at: str,
    index: int,
) -> tuple[str, UUID, str, int]:
    """Ingest, publish, and complete AI work so the incident awaits approval."""

    batch = await incident_store.ingest(normalize_webhook(_webhook(alertname, starts_at)))
    pending = batch.pending_jobs[0]
    await incident_store.mark_job_published(pending.id)
    claim = await incident_store.claim_job(pending.id, pending.incident_id)
    assert claim.claimed
    report = _report(pending.incident_id, index)
    runs = await incident_store.list_runs(pending.incident_id, limit=10, offset=0)
    await artifact_store.save_hypotheses(runs.items[0].id, pending.incident_id, report.hypotheses)
    await artifact_store.save_report(runs.items[0].id, report)
    await incident_store.complete_ai_job(pending.id, report=report)
    detail = await incident_store.get_incident(pending.incident_id)
    assert detail is not None
    assert detail.status == IncidentStatus.WAITING_FOR_APPROVAL
    return pending.incident_id, pending.id, report.recommendations[0].id, detail.version


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_is_concurrency_safe_replayable_and_version_checked(
    migrated_test_database_url: str,
) -> None:
    settings = Settings(_env_file=None, environment="test")
    engine = create_async_engine(migrated_test_database_url)
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
    approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
    try:
        incident_id, _job_id, rec_id, version = await _waiting_incident(
            incident_store,
            artifact_store,
            "IntegrationApprovalRace",
            "2026-09-06T12:00:00Z",
            11,
        )
        first, second = await asyncio.gather(
            approval_store.decide(
                rec_id,
                incident_version=version,
                actor="local-demo-approver",
                decision=ApprovalDecision.APPROVED,
                idempotency_key="race-key-a",
            ),
            approval_store.decide(
                rec_id,
                incident_version=version,
                actor="local-demo-approver",
                decision=ApprovalDecision.APPROVED,
                idempotency_key="race-key-b",
            ),
            return_exceptions=True,
        )
        created = [item for item in (first, second) if not isinstance(item, Exception)]
        conflicts = [item for item in (first, second) if isinstance(item, ApprovalConflict)]
        assert len(created) == 1
        assert len(conflicts) == 1
        winner = created[0]
        assert winner.replayed is False
        winner_key = winner.approval.idempotency_key

        replay = await approval_store.decide(
            rec_id,
            incident_version=version + 1,
            actor="local-demo-approver",
            decision=ApprovalDecision.APPROVED,
            idempotency_key=winner_key,
        )
        assert replay.replayed is True
        assert replay.approval.id == winner.approval.id

        with pytest.raises(ApprovalConflict) as decided:
            await approval_store.decide(
                rec_id,
                incident_version=version,
                actor="local-demo-approver",
                decision=ApprovalDecision.APPROVED,
                idempotency_key="fresh-key",
            )
        # A decided recommendation always conflicts, even with a stale version.
        assert decided.value.code == "approval_conflict"

        _stale_incident, _stale_job, stale_rec, stale_version = await _waiting_incident(
            incident_store,
            artifact_store,
            "IntegrationApprovalStale",
            "2026-09-06T12:30:00Z",
            13,
        )
        with pytest.raises(ApprovalConflict) as stale:
            await approval_store.decide(
                stale_rec,
                incident_version=stale_version - 1,
                actor="local-demo-approver",
                decision=ApprovalDecision.APPROVED,
                idempotency_key="stale-key",
            )
        assert stale.value.code == "stale_version"

        with pytest.raises(ApprovalConflict) as conflict:
            await approval_store.decide(
                rec_id,
                incident_version=version + 1,
                actor="local-demo-approver",
                decision=ApprovalDecision.REJECTED,
                idempotency_key="other-key",
            )
        assert conflict.value.code == "approval_conflict"

        stored = await approval_store.get_approval(rec_id)
        assert stored is not None
        assert stored.decision == ApprovalDecision.APPROVED
        detail = await incident_store.get_incident(incident_id)
        assert detail is not None
        # Approval records the decision without executing remediation: the
        # durable pause persists until a later remediation stage.
        assert detail.status == IncidentStatus.WAITING_FOR_APPROVAL
        assert detail.version == version + 1
        async with engine.connect() as connection:
            approval_count = await connection.scalar(
                select(func.count(ApprovalRow.id)).where(ApprovalRow.recommendation_id == rec_id)
            )
            audit_types = (
                (
                    await connection.execute(
                        select(AuditEventRow.event_type).where(
                            AuditEventRow.incident_id == incident_id,
                            AuditEventRow.event_type == "approval.recorded",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert approval_count == 1
        assert audit_types == ["approval.recorded"]
    finally:
        await approval_store.close()
        await artifact_store.close()
        await incident_store.close()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_moves_incident_to_rejected_with_audit(
    migrated_test_database_url: str,
) -> None:
    settings = Settings(_env_file=None, environment="test")
    engine = create_async_engine(migrated_test_database_url)
    incident_store = SqlAlchemyIncidentStore(settings, engine=engine)
    artifact_store = SqlAlchemyInvestigationStore(settings, engine=engine)
    approval_store = SqlAlchemyApprovalStore(settings, engine=engine)
    try:
        incident_id, _job_id, rec_id, version = await _waiting_incident(
            incident_store,
            artifact_store,
            "IntegrationApprovalReject",
            "2026-09-06T13:00:00Z",
            12,
        )
        response = await approval_store.decide(
            rec_id,
            incident_version=version,
            actor="local-demo-approver",
            decision=ApprovalDecision.REJECTED,
            idempotency_key="reject-key-1",
        )
        assert response.replayed is False
        detail = await incident_store.get_incident(incident_id)
        assert detail is not None
        assert detail.status == IncidentStatus.REJECTED
        recommendations = await artifact_store.list_recommendations(incident_id, limit=10, offset=0)
        assert recommendations.items[0].status == "rejected"
    finally:
        await approval_store.close()
        await artifact_store.close()
        await incident_store.close()
        await engine.dispose()
