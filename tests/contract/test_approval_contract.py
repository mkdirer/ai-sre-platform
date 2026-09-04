"""In-process typed contracts for report reads and human approval decisions."""

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from apps.incident_api.main import create_app
from packages.config import Settings
from packages.models.evidence import EvidenceService
from packages.models.incidents import (
    AuditEventPage,
    IncidentDetail,
    IncidentPage,
    IncidentSeverity,
    IncidentStatus,
    IncidentSummary,
    InvestigationRunPage,
    QueueJobPage,
    QueueJobStatus,
)
from packages.models.investigation import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalResponse,
    Hypothesis,
    HypothesisPage,
    HypothesisStatus,
    IncidentReport,
    Recommendation,
    RecommendationAction,
    RecommendationPage,
    RecommendationRisk,
    ReportStatus,
    RootCauseCategory,
)
from packages.models.knowledge import KnowledgeChunk
from packages.persistence import (
    ApprovalConflict,
    ApprovalNotFound,
    IngestBatch,
)

pytestmark = pytest.mark.contract

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
INCIDENT_ID = "INC-A1B2C3D4E5F60708"
FINGERPRINT = "a" * 64
REC_ID = "REC-A1B2C3D4E5F6070811223344"
REPORT_ID = "RPT-A1B2C3D4E5F6070811223344"
RUN_ID = UUID("7af2ffbd-50fe-42ae-b8be-58ca28fe3f8e")


def _summary() -> IncidentSummary:
    return IncidentSummary(
        id=INCIDENT_ID,
        status=IncidentStatus.WAITING_FOR_APPROVAL,
        title="Payment latency is high",
        service="payment-service",
        affected_services=["payment-service"],
        severity=IncidentSeverity.WARNING,
        started_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
        alert_fingerprint=FINGERPRINT,
        version=3,
        occurrence_count=1,
        root_cause="database_latency",
        confidence=0.8,
    )


def _detail() -> IncidentDetail:
    return IncidentDetail(
        **_summary().model_dump(),
        investigation_window_start=NOW,
        investigation_window_end=NOW,
    )


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        id="HYP-A1B2C3D4E5F6070811223344",
        incident_id=INCIDENT_ID,
        category=RootCauseCategory.DATABASE_LATENCY,
        description="Payment persistence slowed by the injected database delay",
        status=HypothesisStatus.VERIFIED,
        confidence=0.8,
        supporting_evidence_ids=["EVD-A1B2C3D4E5F6070811223344"],
        contradicting_evidence_ids=[],
        reasoning_summary="Latency and spans coincide with the delay notice",
    )


def _recommendation() -> Recommendation:
    return Recommendation(
        id=REC_ID,
        action_type=RecommendationAction.ROLLBACK_DEPLOYMENT,
        target=EvidenceService.PAYMENT,
        parameters={"deployment_id": "DEP-A1B2C3D4E5F607081122", "version": "0.1.0"},
        rationale_evidence_ids=["EVD-A1B2C3D4E5F6070811223344"],
        risk=RecommendationRisk.MEDIUM,
        reversible=True,
        requires_approval=True,
        status="waiting_for_approval",
    )


def _report() -> IncidentReport:
    return IncidentReport(
        id=REPORT_ID,
        incident_id=INCIDENT_ID,
        title="Payment latency is high",
        affected_services=[EvidenceService.PAYMENT],
        severity=IncidentSeverity.WARNING,
        summary="Payment latency is high: deployment regression affecting payment-service",
        root_cause=RootCauseCategory.BAD_DEPLOYMENT,
        root_cause_summary="deployment regression affecting payment-service",
        confidence=0.8,
        timeline=[],
        hypotheses=[_hypothesis()],
        evidence_references=["EVD-A1B2C3D4E5F6070811223344"],
        knowledge_references=[],
        recommendations=[_recommendation()],
        related_incident_ids=[],
        limitations=[],
        status=ReportStatus.WAITING_FOR_APPROVAL,
        generated_at=NOW,
    )


def _approval(replayed: bool = False) -> ApprovalResponse:
    return ApprovalResponse(
        approval=ApprovalRecord(
            id="APR-A1B2C3D4E5F6070811223344",
            incident_id=INCIDENT_ID,
            recommendation_id=REC_ID,
            run_id=str(RUN_ID),
            report_id=REPORT_ID,
            decision=ApprovalDecision.APPROVED,
            actor="local-demo-approver",
            incident_version=3,
            idempotency_key="approval-key-1",
            created_at=NOW,
        ),
        replayed=replayed,
    )


class _FakeStore:
    async def ingest(self, _alerts: tuple[object, ...]) -> IngestBatch:
        raise NotImplementedError

    async def mark_job_published(self, _job_id: UUID) -> None:
        return None

    async def mark_job_publish_failed(self, _job_id: UUID, _error: Exception) -> None:
        return None

    async def list_incidents(
        self, *, limit: int, offset: int, status: IncidentStatus | None = None
    ) -> IncidentPage:
        del limit, offset, status
        raise NotImplementedError

    async def get_incident(self, incident_id: str) -> IncidentDetail | None:
        return _detail() if incident_id == INCIDENT_ID else None

    async def list_timeline(self, _incident_id: str, *, limit: int, offset: int) -> AuditEventPage:
        return AuditEventPage(items=[], total=0, limit=limit, offset=offset)

    async def list_runs(
        self, _incident_id: str, *, limit: int, offset: int
    ) -> InvestigationRunPage:
        return InvestigationRunPage(items=[], total=0, limit=limit, offset=offset)

    async def list_jobs(
        self,
        *,
        limit: int,
        offset: int,
        incident_id: str | None = None,
        status: QueueJobStatus | None = None,
    ) -> QueueJobPage:
        del limit, offset, incident_id, status
        raise NotImplementedError

    async def is_ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class _FakePublisher:
    async def publish(self, *, job_id: UUID, incident_id: str) -> None:
        del job_id, incident_id
        return None


class _FakeQueue:
    async def is_ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class _FakeEvidence:
    async def register_deployment(self, registration):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def list_deployments(self, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def list_evidence(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def all_evidence(self, incident_id: str):  # type: ignore[no-untyped-def]
        return ()

    async def close(self) -> None:
        return None


class _FakeInvestigation:
    def __init__(self, *, absent: bool = False) -> None:
        self._report = None if absent else _report()

    async def get_latest_report(self, incident_id: str) -> IncidentReport | None:
        if incident_id != INCIDENT_ID:
            return None
        return self._report

    async def list_hypotheses(self, incident_id: str, *, limit: int, offset: int) -> HypothesisPage:
        items = [_hypothesis()] if incident_id == INCIDENT_ID else []
        return HypothesisPage(items=items, total=len(items), limit=limit, offset=offset)

    async def list_recommendations(
        self, incident_id: str, *, limit: int, offset: int
    ) -> RecommendationPage:
        items = [_recommendation()] if incident_id == INCIDENT_ID else []
        return RecommendationPage(items=items, total=len(items), limit=limit, offset=offset)

    async def close(self) -> None:
        return None


class _FakeApprovals:
    """Scripted decisions keyed by (recommendation_id, decision)."""

    def __init__(self, *, mode: str = "ok") -> None:
        self.mode = mode
        self.calls: list[tuple[str, ApprovalDecision, int, str]] = []

    async def decide(
        self,
        recommendation_id: str,
        *,
        incident_version: int,
        actor: str,
        decision: ApprovalDecision,
        idempotency_key: str,
    ) -> ApprovalResponse:
        self.calls.append((recommendation_id, decision, incident_version, idempotency_key))
        if recommendation_id != REC_ID:
            raise ApprovalNotFound(f"recommendation {recommendation_id} not found")
        if self.mode == "stale":
            raise ApprovalConflict("stale_version", "incident version 1 is stale (current 3)")
        if self.mode == "conflict":
            raise ApprovalConflict(
                "approval_conflict", "recommendation already has a recorded decision"
            )
        if self.mode == "not_waiting":
            raise ApprovalConflict(
                "not_awaiting_approval",
                "recommendation is approved, not waiting_for_approval",
            )
        if self.mode == "replay":
            return _approval(replayed=True)
        return _approval(replayed=False)

    async def close(self) -> None:
        return None


class _FakeKnowledge:
    async def search(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        if chunk_id != "KNW-A1B2C3D4E5F6070811223344":
            return None
        return KnowledgeChunk(
            id="KNW-A1B2C3D4E5F6070811223344",  # type: ignore[arg-type]
            document_id="DOC-AAAAAAAAAAAAAAAAAAAA",  # type: ignore[arg-type]
            source_path="knowledge/runbooks/payment_database_runbook.md",
            doc_type="runbook",  # type: ignore[arg-type]
            version="v1",
            chunk_index=0,
            text="Disable the fault and verify p95 recovers.",
            embedding=[0.0, 1.0],
            token_estimate=12,
            created_at=NOW,
        )

    async def close(self) -> None:
        return None


class _FakeEmbedder:
    dimensions = 16

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 16 for _ in texts]

    async def close(self) -> None:
        return None


def _client(
    *, investigation: _FakeInvestigation | None = None, approvals: _FakeApprovals | None = None
) -> httpx.AsyncClient:
    app = create_app(
        Settings(_env_file=None, environment="test"),
        store=_FakeStore(),  # type: ignore[arg-type]
        publisher=_FakePublisher(),
        queue_dependency=_FakeQueue(),
        evidence_store=_FakeEvidence(),  # type: ignore[arg-type]
        knowledge_store=_FakeKnowledge(),  # type: ignore[arg-type]
        knowledge_embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        investigation_store=investigation or _FakeInvestigation(),  # type: ignore[arg-type]
        approval_store=approvals or _FakeApprovals(),  # type: ignore[arg-type]
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://incident-api.test",
    )


def _approve_body() -> dict[str, object]:
    return {"incident_version": 3, "actor": "local-demo-approver"}


@pytest.mark.asyncio
async def test_report_read_distinguishes_rca_from_gaps() -> None:
    async with _client() as client:
        response = await client.get(f"/api/v1/incidents/{INCIDENT_ID}/report")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == REPORT_ID
        assert body["root_cause"] == "bad_deployment"
        assert body["status"] == "waiting_for_approval"
        assert body["evidence_references"] == ["EVD-A1B2C3D4E5F6070811223344"]
        assert body["knowledge_references"] == []


@pytest.mark.asyncio
async def test_report_absent_returns_actionable_404() -> None:
    async with _client(investigation=_FakeInvestigation(absent=True)) as client:  # type: ignore[arg-type]
        response = await client.get(f"/api/v1/incidents/{INCIDENT_ID}/report")
        assert response.status_code == 404
        assert response.json()["code"] == "report_absent"


@pytest.mark.asyncio
async def test_hypotheses_and_recommendations_reads() -> None:
    async with _client() as client:
        hypotheses = await client.get(f"/api/v1/incidents/{INCIDENT_ID}/hypotheses")
        assert hypotheses.status_code == 200
        assert hypotheses.json()["items"][0]["status"] == "verified"
        recommendations = await client.get(f"/api/v1/incidents/{INCIDENT_ID}/recommendations")
        assert recommendations.status_code == 200
        item = recommendations.json()["items"][0]
        assert item["status"] == "waiting_for_approval"
        assert item["risk"] == "medium"
        assert item["requires_approval"] is True


@pytest.mark.asyncio
async def test_approve_happy_path_returns_unexecuted_decision() -> None:
    approvals = _FakeApprovals()
    async with _client(approvals=approvals) as client:
        response = await client.post(
            f"/api/v1/recommendations/{REC_ID}/approve",
            json=_approve_body(),
            headers={"Idempotency-Key": "approval-key-1"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["replayed"] is False
        assert body["approval"]["decision"] == "approved"
        assert body["approval"]["actor"] == "local-demo-approver"
        assert approvals.calls == [(REC_ID, ApprovalDecision.APPROVED, 3, "approval-key-1")]


@pytest.mark.asyncio
async def test_reject_happy_path() -> None:
    approvals = _FakeApprovals()
    async with _client(approvals=approvals) as client:
        response = await client.post(
            f"/api/v1/recommendations/{REC_ID}/reject",
            json=_approve_body(),
            headers={"Idempotency-Key": "reject-key-1"},
        )
        assert response.status_code == 200
        assert approvals.calls[0][1] == ApprovalDecision.REJECTED


@pytest.mark.asyncio
async def test_approve_replay_returns_stored_decision() -> None:
    async with _client(approvals=_FakeApprovals(mode="replay")) as client:
        response = await client.post(
            f"/api/v1/recommendations/{REC_ID}/approve",
            json=_approve_body(),
            headers={"Idempotency-Key": "approval-key-1"},
        )
        assert response.status_code == 200
        assert response.json()["replayed"] is True


@pytest.mark.asyncio
async def test_approve_maps_conflicts_to_actionable_409s() -> None:
    for mode, code in [
        ("stale", "stale_version"),
        ("conflict", "approval_conflict"),
        ("not_waiting", "not_awaiting_approval"),
    ]:
        async with _client(approvals=_FakeApprovals(mode=mode)) as client:
            response = await client.post(
                f"/api/v1/recommendations/{REC_ID}/approve",
                json=_approve_body(),
                headers={"Idempotency-Key": "k"},
            )
            assert response.status_code == 409, mode
            assert response.json()["code"] == code, mode


@pytest.mark.asyncio
async def test_approve_requires_idempotency_key_and_known_recommendation() -> None:
    async with _client() as client:
        missing_key = await client.post(
            f"/api/v1/recommendations/{REC_ID}/approve", json=_approve_body()
        )
        assert missing_key.status_code == 400
        assert missing_key.json()["code"] == "missing_idempotency_key"
        unknown = await client.post(
            "/api/v1/recommendations/REC-FFFFFFFFFFFFFFFFFFFFFFFF/approve",
            json=_approve_body(),
            headers={"Idempotency-Key": "k"},
        )
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "recommendation_not_found"


@pytest.mark.asyncio
async def test_malformed_ids_fail_fast_without_reaching_stores() -> None:
    async with _client() as client:
        approve = await client.post(
            "/api/v1/recommendations/not-a-rec-id/approve",
            json=_approve_body(),
            headers={"Idempotency-Key": "k"},
        )
        assert approve.status_code == 422
        reject = await client.post(
            "/api/v1/recommendations/not-a-rec-id/reject",
            json=_approve_body(),
            headers={"Idempotency-Key": "k"},
        )
        assert reject.status_code == 422
        chunk = await client.get("/api/v1/knowledge/chunks/not-a-chunk-id")
        assert chunk.status_code == 422


@pytest.mark.asyncio
async def test_whitespace_actor_is_rejected() -> None:
    async with _client() as client:
        response = await client.post(
            f"/api/v1/recommendations/{REC_ID}/approve",
            json={"incident_version": 3, "actor": "   "},
            headers={"Idempotency-Key": "k"},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_knowledge_chunk_read_supports_related_knowledge() -> None:
    async with _client() as client:
        response = await client.get("/api/v1/knowledge/chunks/KNW-A1B2C3D4E5F6070811223344")
        assert response.status_code == 200
        assert response.json()["source_path"].endswith("payment_database_runbook.md")
        missing = await client.get("/api/v1/knowledge/chunks/KNW-FFFFFFFFFFFFFFFFFFFFFFFF")
        assert missing.status_code == 404
        assert missing.json()["code"] == "knowledge_chunk_not_found"


@pytest.mark.asyncio
async def test_openapi_documents_approval_reads() -> None:
    async with _client() as client:
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        for path in [
            "/api/v1/incidents/{incident_id}/report",
            "/api/v1/incidents/{incident_id}/hypotheses",
            "/api/v1/incidents/{incident_id}/recommendations",
            "/api/v1/recommendations/{recommendation_id}/approve",
            "/api/v1/recommendations/{recommendation_id}/reject",
            "/api/v1/knowledge/chunks/{chunk_id}",
        ]:
            assert path in paths, path
