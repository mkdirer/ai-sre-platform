"""Unit coverage for approval-gated remediation execution (Stage 10)."""

from datetime import UTC, datetime

import httpx
import pytest

from packages.config import Settings
from packages.models.deployments import DeploymentEnvironment, DeploymentRecord
from packages.models.evidence import EvidenceService
from packages.models.investigation import RecommendationAction
from packages.models.remediation import (
    ExecutionStatus,
    ForbiddenRemediationAction,
    RecommendationContext,
    RemediationExecution,
    RollbackDeploymentParams,
)
from packages.remediation.adapter import (
    AdapterOutcome,
    AdapterResult,
    PaymentServiceRollbackAdapter,
    resolve_service_base_url,
)
from packages.remediation.registry import action_name_for, validate_rollback_params
from packages.remediation.service import ExecutionOutcome, RemediationExecutionService


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "telemetry_enabled": False,
        "fault_control_token": "unit-remediation-token",
        "payment_service_url": "http://payment:8000",
        "remediation_verification_window_seconds": 2.0,
        "remediation_verification_poll_seconds": 0.01,
        "remediation_required_healthy_polls": 2,
    }
    values.update(overrides)
    return Settings(**values)


def _params(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "service": "payment-service",
        "deployment_id": "DEP-AAAAAAAAAAAAAAAAAAAA",
        "version": "0.1.0",
    }
    values.update(overrides)
    return values


def _execution(**overrides: object) -> RemediationExecution:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": "REM-AAAAAAAAAAAAAAAAAAAAAAAA",
        "incident_id": "INC-AAAAAAAAAAAAAAAA",
        "recommendation_id": "REC-AAAAAAAAAAAAAAAAAAAAAAAA",
        "approval_id": "APR-AAAAAAAAAAAAAAAAAAAAAAAA",
        "action_type": RecommendationAction.ROLLBACK_DEPLOYMENT,
        "action_name": "rollback_payment_deployment",
        "target": EvidenceService.PAYMENT,
        "incident_version": 3,
        "status": ExecutionStatus.PENDING,
        "attempts": 0,
        "stop_requested": False,
        "result": {},
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return RemediationExecution.model_validate(values)


def _deployment(version: str, deployment_id: str = "DEP-AAAAAAAAAAAAAAAAAAAA") -> DeploymentRecord:
    return DeploymentRecord.model_validate(
        {
            "id": deployment_id,
            "service": "payment-service",
            "environment": "development",
            "version": version,
            "deployed_at": "2026-09-05T12:00:00Z",
            "commit_sha": "1" * 40,
            "registered_at": "2026-09-05T12:00:00Z",
        }
    )


class _FakeStore:
    """Minimal execution store mirroring the real status guards."""

    def __init__(self, execution: RemediationExecution) -> None:
        self.execution = execution
        self.events: list[str] = []

    async def get_execution(self, execution_id: str) -> RemediationExecution | None:
        assert execution_id == self.execution.id
        return self.execution

    def _move(self, status: ExecutionStatus, event: str) -> RemediationExecution:
        self.execution = self.execution.model_copy(update={"status": status})
        self.events.append(event)
        return self.execution

    async def mark_executing(self, execution_id: str, *, actor: str) -> RemediationExecution:
        assert execution_id == self.execution.id
        assert self.execution.status == ExecutionStatus.PENDING
        return self._move(ExecutionStatus.EXECUTING, "executing")

    async def mark_verifying(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution:
        assert self.execution.status == ExecutionStatus.EXECUTING
        return self._move(ExecutionStatus.VERIFYING, "verifying")

    async def mark_completed(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution:
        assert self.execution.status == ExecutionStatus.VERIFYING
        return self._move(ExecutionStatus.COMPLETED, "completed")

    async def mark_ambiguous(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution:
        assert self.execution.status == ExecutionStatus.VERIFYING
        return self._move(ExecutionStatus.VERIFYING, "ambiguous")

    async def mark_failed(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution:
        assert self.execution.status in (
            ExecutionStatus.PENDING,
            ExecutionStatus.EXECUTING,
            ExecutionStatus.VERIFYING,
        )
        return self._move(ExecutionStatus.FAILED, "failed")


class _FakeContexts:
    def __init__(self, context: RecommendationContext) -> None:
        self._context = context

    async def recommendation_for(self, execution_id: str) -> RecommendationContext:
        return self._context


class _FakeDeployments:
    def __init__(self, records: tuple[DeploymentRecord, ...]) -> None:
        self._records = records
        self.registered: list[str] = []

    async def current_previous_deployments(
        self, *, service: EvidenceService, environment: DeploymentEnvironment, at: object
    ) -> tuple[DeploymentRecord, ...]:
        return self._records

    async def register_deployment(self, registration: object) -> object:
        self.registered.append("rollback")
        return None


class _FakeAdapter:
    def __init__(self, outcomes: list[AdapterResult]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def disable_faults(self, params: RollbackDeploymentParams) -> AdapterResult:
        self.calls += 1
        return self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]


def _context(**overrides: object) -> RecommendationContext:
    values: dict[str, object] = {
        "action_type": RecommendationAction.ROLLBACK_DEPLOYMENT,
        "target": EvidenceService.PAYMENT,
        "parameters": _params(),
    }
    values.update(overrides)
    return RecommendationContext(
        action_type=values["action_type"],  # type: ignore[arg-type]
        target=values["target"],  # type: ignore[arg-type]
        parameters=values["parameters"],  # type: ignore[arg-type]
    )


def _service(
    store: _FakeStore,
    *,
    context: RecommendationContext | None = None,
    records: tuple[DeploymentRecord, ...] | None = None,
    outcomes: list[AdapterResult] | None = None,
    samples: list[float | None] | None = None,
    settings: Settings | None = None,
) -> tuple[RemediationExecutionService, _FakeAdapter]:
    adapter = _FakeAdapter(outcomes or [AdapterResult(AdapterOutcome.APPLIED, detail="ok")])
    script = list(samples if samples is not None else [0.05, 0.05])

    async def probe(service: EvidenceService) -> float | None:
        return script.pop(0) if len(script) > 1 else script[0]

    async def sleeper(delay: float) -> None:
        return None

    service = RemediationExecutionService(
        store,  # type: ignore[arg-type]
        _FakeContexts(context or _context()),
        _FakeDeployments(
            records
            if records is not None
            else (
                _deployment("0.2.0", "DEP-BBBBBBBBBBBBBBBBBBBB"),
                _deployment("0.1.0"),
            )
        ),
        adapter,
        settings or _settings(),
        latency_probe=probe,
        sleeper=sleeper,
    )
    return service, adapter


def test_registry_accepts_payment_rollback() -> None:
    """The one executable action validates into typed parameters."""

    assert action_name_for(RecommendationAction.ROLLBACK_DEPLOYMENT) == (
        "rollback_payment_deployment"
    )
    params = validate_rollback_params(
        action=RecommendationAction.ROLLBACK_DEPLOYMENT,
        target=EvidenceService.PAYMENT,
        parameters=_params(),
    )
    assert params.version == "0.1.0"
    assert params.deployment_id == "DEP-AAAAAAAAAAAAAAAAAAAA"


def test_registry_rejects_non_executable_actions() -> None:
    """NO_ACTION and INVESTIGATE_DATABASE can never execute."""

    for action in (RecommendationAction.NO_ACTION, RecommendationAction.INVESTIGATE_DATABASE):
        with pytest.raises(ForbiddenRemediationAction) as exc_info:
            validate_rollback_params(
                action=action, target=EvidenceService.PAYMENT, parameters=_params()
            )
        assert exc_info.value.code == "forbidden_action"


def test_registry_rejects_foreign_target_and_faults() -> None:
    """Cross-service targets, unknown faults, and empty params are forbidden."""

    with pytest.raises(ForbiddenRemediationAction):
        validate_rollback_params(
            action=RecommendationAction.ROLLBACK_DEPLOYMENT,
            target=EvidenceService.INVENTORY,
            parameters=_params(),
        )
    with pytest.raises(ForbiddenRemediationAction):
        validate_rollback_params(
            action=RecommendationAction.ROLLBACK_DEPLOYMENT,
            target=EvidenceService.PAYMENT,
            parameters=_params(deployment_id="DEP-BAD"),
        )
    with pytest.raises(ForbiddenRemediationAction):
        validate_rollback_params(
            action=RecommendationAction.ROLLBACK_DEPLOYMENT,
            target=EvidenceService.PAYMENT,
            parameters=_params(version=""),
        )


def test_adapter_resolves_only_payment() -> None:
    """Endpoint resolution is allowlisted; the model supplies no URL."""

    settings = _settings()
    assert resolve_service_base_url(settings, EvidenceService.PAYMENT) == "http://payment:8000"
    with pytest.raises(ValueError, match="no registered control endpoint"):
        resolve_service_base_url(settings, EvidenceService.INVENTORY)


def _adapter_client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_adapter_disables_and_confirms() -> None:
    """Enabled faults are disabled and confirmed; disabled faults need no PUT."""

    import asyncio

    calls: list[str] = []
    enabled = {"bad-deployment"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        name = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET":
            return httpx.Response(200, json={"enabled": name in enabled})
        enabled.discard(name)
        return httpx.Response(200, json={"enabled": False})

    async def _run() -> AdapterResult:
        adapter = PaymentServiceRollbackAdapter(_settings(), client=_adapter_client(handler))
        return await adapter.disable_faults(
            RollbackDeploymentParams.model_validate(
                {
                    "service": "payment-service",
                    "deployment_id": "DEP-AAAAAAAAAAAAAAAAAAAA",
                    "version": "0.1.0",
                }
            )
        )

    result = asyncio.run(_run())
    assert result.outcome == AdapterOutcome.APPLIED
    assert "PUT /internal/faults/bad-deployment" in calls
    assert "PUT /internal/faults/slow-database" not in calls
    assert not enabled


def test_adapter_reports_unknown_when_write_does_not_land() -> None:
    """A 2xx that leaves the fault enabled is unknown, never success."""

    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"enabled": True})
        return httpx.Response(200, json={"enabled": False})

    async def _run() -> AdapterResult:
        adapter = PaymentServiceRollbackAdapter(_settings(), client=_adapter_client(handler))
        return await adapter.disable_faults(
            RollbackDeploymentParams.model_validate(
                {
                    "service": "payment-service",
                    "deployment_id": "DEP-AAAAAAAAAAAAAAAAAAAA",
                    "version": "0.1.0",
                }
            )
        )

    result = asyncio.run(_run())
    assert result.outcome == AdapterOutcome.UNKNOWN


def test_adapter_reports_already_applied_when_nothing_enabled() -> None:
    """A fully rolled-back service is reported honestly, not as a fresh apply."""

    import asyncio

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"enabled": False})

    async def _run() -> AdapterResult:
        adapter = PaymentServiceRollbackAdapter(_settings(), client=_adapter_client(handler))
        return await adapter.disable_faults(
            RollbackDeploymentParams.model_validate(
                {
                    "service": "payment-service",
                    "deployment_id": "DEP-AAAAAAAAAAAAAAAAAAAA",
                    "version": "0.1.0",
                }
            )
        )

    result = asyncio.run(_run())
    assert result.outcome == AdapterOutcome.ALREADY_APPLIED
    assert not [call for call in calls if call.startswith("PUT")]


def test_adapter_settles_timeout_by_readback() -> None:
    """A timeout after send is unknown until the read-back settles it."""

    import asyncio

    states = {"enabled": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"enabled": states["enabled"]})
        states["enabled"] = False
        raise httpx.ReadTimeout("send ambiguous", request=request)

    async def _run() -> AdapterResult:
        adapter = PaymentServiceRollbackAdapter(_settings(), client=_adapter_client(handler))
        return await adapter.disable_faults(
            RollbackDeploymentParams.model_validate(
                {
                    "service": "payment-service",
                    "deployment_id": "DEP-AAAAAAAAAAAAAAAAAAAA",
                    "version": "0.1.0",
                }
            )
        )

    result = asyncio.run(_run())
    assert result.outcome == AdapterOutcome.APPLIED


def test_adapter_rejects_forbidden_control() -> None:
    """401/403/404 from the control plane is forbidden, never retried as success."""

    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"enabled": True})
        return httpx.Response(401, json={"code": "fault_control_unauthorized"})

    async def _run() -> AdapterResult:
        adapter = PaymentServiceRollbackAdapter(_settings(), client=_adapter_client(handler))
        return await adapter.disable_faults(
            RollbackDeploymentParams.model_validate(
                {
                    "service": "payment-service",
                    "deployment_id": "DEP-AAAAAAAAAAAAAAAAAAAA",
                    "version": "0.1.0",
                }
            )
        )

    result = asyncio.run(_run())
    assert result.outcome == AdapterOutcome.FORBIDDEN


def test_service_completes_on_verified_recovery() -> None:
    """Adapter run plus healthy polls completes the execution."""

    import asyncio

    store = _FakeStore(_execution())
    service, adapter = _service(store)

    result = asyncio.run(service.execute(execution_id=store.execution.id, actor="op"))
    assert result.outcome == ExecutionOutcome.COMPLETED
    assert store.execution.status == ExecutionStatus.COMPLETED
    assert adapter.calls == 1
    assert store.events == ["executing", "verifying", "completed"]


def test_service_rejects_forbidden_without_touching_adapter() -> None:
    """Forbidden parameters fail closed before any mutation attempt."""

    import asyncio

    store = _FakeStore(_execution())
    service, adapter = _service(store, context=_context(action_type=RecommendationAction.NO_ACTION))

    result = asyncio.run(service.execute(execution_id=store.execution.id, actor="op"))
    assert result.outcome == ExecutionOutcome.FAILED
    assert adapter.calls == 0
    assert store.execution.status == ExecutionStatus.FAILED


def test_service_rejects_stale_deployment_state() -> None:
    """A version mismatch against the registry fails without executing."""

    import asyncio

    store = _FakeStore(_execution())
    service, adapter = _service(
        store,
        records=(
            _deployment("0.3.0", "DEP-CCCCCCCCCCCCCCCCCCCC"),
            _deployment("0.2.0", "DEP-BBBBBBBBBBBBBBBBBBBB"),
        ),
    )

    result = asyncio.run(service.execute(execution_id=store.execution.id, actor="op"))
    assert result.outcome == ExecutionOutcome.FAILED
    assert result.detail == "stale deployment state"
    assert adapter.calls == 0


def test_service_fails_after_bounded_adapter_retries() -> None:
    """Partial failure retries bounded times, then fails unresolved."""

    import asyncio

    store = _FakeStore(_execution())
    service, adapter = _service(
        store,
        outcomes=[AdapterResult(AdapterOutcome.FAILED, detail="refused")],
        settings=_settings(remediation_execution_max_attempts=2),
    )

    result = asyncio.run(service.execute(execution_id=store.execution.id, actor="op"))
    assert result.outcome == ExecutionOutcome.FAILED
    assert adapter.calls == 2
    assert store.execution.status == ExecutionStatus.FAILED


def test_service_stays_verifying_without_recovery() -> None:
    """Sustained high latency ends ambiguous; the incident is never resolved."""

    import asyncio

    store = _FakeStore(_execution())
    service, _ = _service(store, samples=[2.5, 2.6, 2.7, 2.8])

    result = asyncio.run(service.execute(execution_id=store.execution.id, actor="op"))
    assert result.outcome == ExecutionOutcome.AMBIGUOUS
    assert store.execution.status == ExecutionStatus.VERIFYING
    assert "gaps" in result.detail or "p95" in result.detail


def test_service_treats_provider_outage_as_gaps() -> None:
    """Unavailable telemetry yields gaps, never a resolved incident."""

    import asyncio

    store = _FakeStore(_execution())
    service, _ = _service(
        store,
        samples=[None, None, None, None],
        settings=_settings(remediation_verification_window_seconds=0.2),
    )

    result = asyncio.run(service.execute(execution_id=store.execution.id, actor="op"))
    assert result.outcome == ExecutionOutcome.AMBIGUOUS
    assert store.execution.status == ExecutionStatus.VERIFYING
    assert "unavailable" in result.detail or "no telemetry" in result.detail


def test_service_observes_manual_stop() -> None:
    """A flagged stop ends verification without resolving.

    Production stops commit synchronously in the store; the loop honors a
    leftover flag defensively so a racing stop can never resolve.
    """

    import asyncio

    store = _FakeStore(_execution(stop_requested=True))
    service, _ = _service(store, samples=[2.5, 2.5])

    result = asyncio.run(service.execute(execution_id=store.execution.id, actor="op"))
    assert result.outcome == ExecutionOutcome.STOPPED
    assert result.execution.stop_requested is True
    assert store.execution.status != ExecutionStatus.COMPLETED


def test_service_skips_non_pending_redelivery() -> None:
    """At-least-once redelivery after claim is an idempotent no-op."""

    import asyncio

    store = _FakeStore(_execution(status=ExecutionStatus.EXECUTING))
    service, adapter = _service(store)

    result = asyncio.run(service.execute(execution_id=store.execution.id, actor="op"))
    assert result.outcome == ExecutionOutcome.SKIPPED_IDEMPOTENT
    assert adapter.calls == 0


def test_service_marks_failed_on_unexpected_adapter_error() -> None:
    """Only a process crash escapes termination; anything else fails closed.

    The failure is persisted for the operator while the exception still
    propagates so the worker task itself is visibly failed, not silent.
    """

    import asyncio

    import pytest

    class _ExplodingAdapter:
        async def disable_faults(self, params: RollbackDeploymentParams) -> AdapterResult:
            raise RuntimeError("adapter blew up")

    store = _FakeStore(_execution())
    service, _ = _service(store)
    service._adapter = _ExplodingAdapter()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="adapter blew up"):
        asyncio.run(service.execute(execution_id=store.execution.id, actor="op"))
    assert store.execution.status == ExecutionStatus.FAILED
