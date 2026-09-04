"""Contract coverage for approval-gated remediation endpoints (Stage 10)."""

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from apps.incident_api.main import create_app
from packages.config import Settings
from packages.models.evidence import EvidenceService
from packages.models.investigation import RecommendationAction
from packages.models.remediation import ExecutionStatus, RemediationExecution
from packages.persistence import (
    RemediationConflict,
    RemediationNotFound,
    RemediationStoreUnavailable,
)
from packages.task_queue import JobPublishError

pytestmark = pytest.mark.contract

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
REC_ID = "REC-A1B2C3D4E5F6070811223344"
EXEC_ID = "REM-A1B2C3D4E5F6070811223344"
KEY = "exec-contract-key"


def _execution(**overrides: object) -> RemediationExecution:
    values: dict[str, object] = {
        "id": EXEC_ID,
        "incident_id": "INC-A1B2C3D4E5F60708",
        "recommendation_id": REC_ID,
        "approval_id": "APR-A1B2C3D4E5F6070811223344",
        "action_type": RecommendationAction.ROLLBACK_DEPLOYMENT,
        "action_name": "rollback_payment_deployment",
        "target": EvidenceService.PAYMENT,
        "incident_version": 3,
        "status": ExecutionStatus.PENDING,
        "attempts": 0,
        "stop_requested": False,
        "result": {},
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return RemediationExecution.model_validate(values)


class _FakeRemediation:
    """Scripted execution claims keyed by test mode."""

    def __init__(self, *, mode: str = "ok") -> None:
        self.mode = mode
        self.published = 0

    async def request_execution(
        self,
        recommendation_id: str,
        *,
        incident_version: int,
        expected_service_version: str,
        actor: str,
        idempotency_key: str,
    ) -> tuple[RemediationExecution, bool]:
        del incident_version, expected_service_version, actor
        if recommendation_id != REC_ID:
            raise RemediationNotFound(f"recommendation {recommendation_id} not found")
        if self.mode == "stale":
            raise RemediationConflict("stale_version", "incident version 1 is stale")
        if self.mode == "conflict":
            raise RemediationConflict("execution_in_progress", "remediation is executing")
        if self.mode == "forbidden":
            raise RemediationConflict("forbidden_action", "action not in the registry")
        if self.mode == "unavailable":
            raise RemediationStoreUnavailable("db down")
        if self.mode == "replay":
            return _execution(), True
        return _execution(), False

    async def get_execution(self, execution_id: str) -> RemediationExecution | None:
        if self.mode == "unavailable":
            raise RemediationStoreUnavailable("db down")
        if execution_id != EXEC_ID:
            return None
        return _execution()

    async def request_stop(
        self, execution_id: str, *, incident_version: int, actor: str
    ) -> RemediationExecution:
        del incident_version, actor
        if execution_id != EXEC_ID:
            raise RemediationNotFound(f"execution {execution_id} not found")
        if self.mode == "completed":
            raise RemediationConflict("already_completed", "execution is completed")
        return _execution(stop_requested=True, status=ExecutionStatus.STOPPED)

    async def mark_failed(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution:
        del actor, details
        if self.mode == "store-down":
            raise RemediationStoreUnavailable("db down")
        return _execution(id=execution_id, status=ExecutionStatus.FAILED)

    async def close(self) -> None:
        return None


class _FakePublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.remediation_calls = 0

    async def publish(self, *, job_id: UUID, incident_id: str) -> None:
        del job_id, incident_id
        return None

    async def publish_remediation(
        self, *, task_id: UUID, execution_id: str, incident_id: str
    ) -> None:
        del task_id, execution_id, incident_id
        self.remediation_calls += 1
        if self.fail:
            raise JobPublishError("broker down")


def _client(
    remediation: _FakeRemediation | None = None, publisher: _FakePublisher | None = None
) -> tuple[httpx.AsyncClient, _FakePublisher]:
    fake_publisher = publisher or _FakePublisher()
    app = create_app(
        Settings(_env_file=None, environment="test"),
        publisher=fake_publisher,  # type: ignore[arg-type]
        remediation_store=remediation or _FakeRemediation(),  # type: ignore[arg-type]
    )
    return (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://incident-api.test"
        ),
        fake_publisher,
    )


def _body() -> dict[str, object]:
    return {
        "incident_version": 3,
        "expected_service_version": "0.2.0",
        "actor": "local-demo-approver",
    }


@pytest.mark.asyncio
async def test_execute_claims_and_enqueues() -> None:
    """Approved rollback claims an execution and publishes exactly one task."""

    client, publisher = _client()
    async with client:
        response = await client.post(
            f"/api/v1/recommendations/{REC_ID}/execute",
            json=_body(),
            headers={"Idempotency-Key": KEY},
        )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["execution"]["id"] == EXEC_ID
    assert payload["replayed"] is False
    assert publisher.remediation_calls == 1


@pytest.mark.asyncio
async def test_execute_replay_skips_publish() -> None:
    """Replays return the stored execution without a second task."""

    client, publisher = _client(_FakeRemediation(mode="replay"))
    async with client:
        response = await client.post(
            f"/api/v1/recommendations/{REC_ID}/execute",
            json=_body(),
            headers={"Idempotency-Key": KEY},
        )
    assert response.status_code == 202
    assert response.json()["replayed"] is True
    assert publisher.remediation_calls == 0


@pytest.mark.asyncio
async def test_execute_rejections_map_to_status_codes() -> None:
    """Unknown, stale, conflicting, and forbidden claims map deterministically."""

    cases = [
        ("missing", "REC-FFFFFFFFFFFFFFFFFFFFFFFF", 404, "recommendation_not_found"),
        ("stale", REC_ID, 409, "stale_version"),
        ("conflict", REC_ID, 409, "execution_in_progress"),
        ("forbidden", REC_ID, 409, "forbidden_action"),
    ]
    for mode, rec_id, expected_status, expected_code in cases:
        remediation = _FakeRemediation(mode="ok" if mode == "missing" else mode)
        client, _ = _client(remediation)
        async with client:
            response = await client.post(
                f"/api/v1/recommendations/{rec_id}/execute",
                json=_body(),
                headers={"Idempotency-Key": KEY},
            )
        assert response.status_code == expected_status, (mode, response.text)
        assert response.json()["code"] == expected_code, mode


@pytest.mark.asyncio
async def test_execute_publish_failure_marks_failed() -> None:
    """Queue outage fails the claim visibly instead of stranding it pending."""

    client, _ = _client(_FakeRemediation(), _FakePublisher(fail=True))
    async with client:
        response = await client.post(
            f"/api/v1/recommendations/{REC_ID}/execute",
            json=_body(),
            headers={"Idempotency-Key": KEY},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "queue_unavailable"


@pytest.mark.asyncio
async def test_execute_publish_and_store_double_fault() -> None:
    """A store outage during publish-failure handling stays a typed 503."""

    client, _ = _client(_FakeRemediation(mode="store-down"), _FakePublisher(fail=True))
    async with client:
        response = await client.post(
            f"/api/v1/recommendations/{REC_ID}/execute",
            json=_body(),
            headers={"Idempotency-Key": KEY},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "persistence_unavailable"


@pytest.mark.asyncio
async def test_get_and_stop_execution() -> None:
    """Execution reads and manual stop are typed and guarded."""

    client, _ = _client()
    async with client:
        found = await client.get(f"/api/v1/remediations/{EXEC_ID}")
        assert found.status_code == 200
        assert found.json()["id"] == EXEC_ID
        missing = await client.get("/api/v1/remediations/REM-FFFFFFFFFFFFFFFFFFFFFFFF")
        assert missing.status_code == 404
        stopped = await client.post(
            f"/api/v1/remediations/{EXEC_ID}/stop",
            json={"incident_version": 4, "actor": "local-demo-approver"},
            headers={"Idempotency-Key": "stop-key"},
        )
        assert stopped.status_code == 200
        assert stopped.json()["stop_requested"] is True
        assert stopped.json()["status"] == "stopped"


@pytest.mark.asyncio
async def test_stop_terminal_conflicts() -> None:
    """Stopping a terminal execution is a conflict, not a silent no-op."""

    client, _ = _client(_FakeRemediation(mode="completed"))
    async with client:
        response = await client.post(
            f"/api/v1/remediations/{EXEC_ID}/stop",
            json={"incident_version": 4, "actor": "local-demo-approver"},
            headers={"Idempotency-Key": "stop-key"},
        )
    assert response.status_code == 409
    assert response.json()["code"] == "already_completed"


@pytest.mark.asyncio
async def test_execute_requires_idempotency_key() -> None:
    """Missing keys are rejected before any claim is attempted."""

    remediation = _FakeRemediation()
    client, publisher = _client(remediation)
    async with client:
        response = await client.post(f"/api/v1/recommendations/{REC_ID}/execute", json=_body())
    assert response.status_code == 400
    assert publisher.remediation_calls == 0
