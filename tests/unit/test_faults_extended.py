"""Unit coverage for extended bounded fault controls (Stage 09)."""

import pytest

from apps.demo.common.faults import (
    FaultControlDisabledError,
    FaultControlUnauthorizedError,
    MultiFaultController,
)
from packages.config import Settings
from packages.models.faults import INVENTORY_FAULTS, PAYMENT_FAULTS, FaultName
from packages.telemetry import TelemetryRuntime


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "telemetry_enabled": False,
        "fault_injection_allowed": True,
        "fault_control_token": "unit-test-control-token",
    }
    values.update(overrides)
    return Settings(**values)


def _payment_controller(settings: Settings, **kwargs: object) -> MultiFaultController:
    runtime = TelemetryRuntime.create(service_name="payment-service", settings=settings)
    return MultiFaultController.from_settings(
        settings,
        service_name="payment-service",
        faults=PAYMENT_FAULTS,
        telemetry=runtime,
        **kwargs,  # type: ignore[arg-type]
    )


def test_all_new_faults_initialize_disabled() -> None:
    """Every allowlisted fault starts off and stays reversible."""

    controller = _payment_controller(_settings())
    for fault in PAYMENT_FAULTS:
        assert controller.is_enabled(fault) is False
        assert controller.state(fault).allowed is True
    controller.set_enabled(FaultName.POOL_EXHAUSTION, True)
    assert controller.is_enabled(FaultName.POOL_EXHAUSTION) is True
    controller.disable_all()
    for fault in PAYMENT_FAULTS:
        assert controller.is_enabled(fault) is False


def test_fault_control_denied_in_production() -> None:
    """Production plus an explicit opt-in still denies every new fault."""

    controller = _payment_controller(_settings(environment="production"))
    assert controller.state(FaultName.CPU_SATURATION).allowed is False
    with pytest.raises(FaultControlDisabledError):
        controller.authorize("unit-test-control-token")


def test_fault_control_requires_exact_token() -> None:
    """Missing and incorrect credentials fail without changing state."""

    controller = _payment_controller(_settings())
    with pytest.raises(FaultControlUnauthorizedError):
        controller.authorize(None)
    with pytest.raises(FaultControlUnauthorizedError):
        controller.authorize("incorrect")
    with pytest.raises(FaultControlUnauthorizedError):
        controller.authorize("tökén-non-ascii")
    assert controller.is_enabled(FaultName.HIGH_ERROR_RATE) is False


def test_inventory_only_owns_its_timeout() -> None:
    """Inventory exposes exactly one allowlisted fault."""

    assert INVENTORY_FAULTS == (FaultName.INVENTORY_TIMEOUT,)
    settings = _settings()
    runtime = TelemetryRuntime.create(service_name="inventory-service", settings=settings)
    controller = MultiFaultController.from_settings(
        settings,
        service_name="inventory-service",
        faults=INVENTORY_FAULTS,
        telemetry=runtime,
    )
    assert controller.state(FaultName.INVENTORY_TIMEOUT).service == "inventory-service"
    assert controller.state(FaultName.INVENTORY_TIMEOUT).delay_seconds == 1.5


@pytest.mark.asyncio
async def test_delay_faults_use_bounded_configured_delays() -> None:
    """Simulated delays honor validated settings and never run unbounded work."""

    delays: list[float] = []
    states: list[tuple[str, bool]] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def callback(fault: FaultName, enabled: bool) -> None:
        states.append((fault.value, enabled))

    settings = _settings()
    runtime = TelemetryRuntime.create(service_name="payment-service", settings=settings)
    controller = MultiFaultController.from_settings(
        settings,
        service_name="payment-service",
        faults=PAYMENT_FAULTS,
        telemetry=runtime,
        sleeper=sleeper,
        state_callback=callback,
    )
    controller.authorize("unit-test-control-token")
    controller.set_enabled(FaultName.POOL_EXHAUSTION, True)
    await controller.inject_delay(FaultName.POOL_EXHAUSTION)
    controller.set_enabled(FaultName.POOL_EXHAUSTION, False)
    await controller.inject_delay(FaultName.POOL_EXHAUSTION)

    assert delays == [1.0]
    assert ("pool_exhaustion", True) in states
    assert ("pool_exhaustion", False) in states


@pytest.mark.asyncio
async def test_cpu_saturation_is_bounded_simulation() -> None:
    """CPU fault completes quickly with a short bounded delay (no host burn)."""

    import time

    settings = _settings()
    runtime = TelemetryRuntime.create(service_name="payment-service", settings=settings)
    controller = MultiFaultController.from_settings(
        settings,
        service_name="payment-service",
        faults=PAYMENT_FAULTS,
        telemetry=runtime,
    )
    controller.set_enabled(FaultName.CPU_SATURATION, True)
    started = time.perf_counter()
    await controller.inject_delay(FaultName.CPU_SATURATION)
    elapsed = time.perf_counter() - started
    # Simulated 0.2s delay plus a tiny hash loop must stay well under 2s.
    assert elapsed < 2.0


def test_high_error_rate_is_deterministic_and_reversible() -> None:
    """Error injection is a stable function of the key and stops when disabled."""

    controller = _payment_controller(_settings())
    controller.set_enabled(FaultName.HIGH_ERROR_RATE, True)
    first = controller.should_inject_error(FaultName.HIGH_ERROR_RATE, "eval-key-1")
    second = controller.should_inject_error(FaultName.HIGH_ERROR_RATE, "eval-key-1")
    assert first == second
    # Both outcomes occur across keys (roughly half), proving determinism.
    outcomes = {
        controller.should_inject_error(FaultName.HIGH_ERROR_RATE, f"eval-key-{index}")
        for index in range(20)
    }
    assert outcomes == {True, False}
    controller.set_enabled(FaultName.HIGH_ERROR_RATE, False)
    assert controller.should_inject_error(FaultName.HIGH_ERROR_RATE, "eval-key-1") is False


def test_fault_metric_labels_are_allowlisted() -> None:
    """Gauge publication rejects arbitrary fault labels."""

    from packages.telemetry.metrics import HttpMetrics

    metrics = HttpMetrics("payment-service")
    metrics.set_fault_enabled("pool_exhaustion", True)
    exposition = metrics.render().decode()
    assert 'demo_fault_enabled{fault="pool_exhaustion",service="payment-service"} 1.0' in exposition
    with pytest.raises(ValueError, match="unsupported fault"):
        metrics.set_fault_enabled("arbitrary_exec", True)


@pytest.mark.asyncio
async def test_payment_app_generic_fault_endpoints_are_guarded() -> None:
    """Payment exposes allowlisted generic controls with the same auth boundary."""

    import httpx

    from apps.demo.payment_service.main import create_app
    from tests.fakes import FakePaymentStore

    settings = _settings(fault_control_token="contract-fault-token")
    app = create_app(settings, store=FakePaymentStore())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        unauthorized = await client.put("/internal/faults/pool-exhaustion", json={"enabled": True})
        assert unauthorized.status_code == 401
        headers = {"X-Fault-Control-Token": "contract-fault-token"}
        enabled = await client.put(
            "/internal/faults/pool-exhaustion", json={"enabled": True}, headers=headers
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert enabled.json()["name"] == "pool_exhaustion"
        listing = await client.get("/internal/faults", headers=headers)
        assert listing.status_code == 200
        assert len(listing.json()["faults"]) == len(PAYMENT_FAULTS)
        unknown = await client.get("/internal/faults/not-a-fault", headers=headers)
        assert unknown.status_code == 404
        other_service = await client.get("/internal/faults/inventory-timeout", headers=headers)
        assert other_service.status_code == 404
        # Legacy alias still works.
        legacy = await client.get("/internal/faults/slow-database", headers=headers)
        assert legacy.status_code == 200


@pytest.mark.asyncio
async def test_inventory_app_timeout_fault_is_bounded() -> None:
    """Inventory timeout control is guarded, reversible, and service-scoped."""

    import httpx

    from apps.demo.inventory_service.main import create_app

    settings = _settings(fault_control_token="inventory-token")
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        headers = {"X-Fault-Control-Token": "inventory-token"}
        enabled = await client.put(
            "/internal/faults/inventory-timeout", json={"enabled": True}, headers=headers
        )
        assert enabled.status_code == 200
        assert enabled.json()["delay_seconds"] == 1.5
        disabled = await client.put(
            "/internal/faults/inventory-timeout", json={"enabled": False}, headers=headers
        )
        assert disabled.json()["enabled"] is False
        wrong_service = await client.put(
            "/internal/faults/slow-database", json={"enabled": True}, headers=headers
        )
        assert wrong_service.status_code == 404


def test_activated_fault_disables_on_body_failure() -> None:
    """The live-runner fault guard disables even when the scenario body raises."""

    import asyncio
    import json

    import httpx

    from packages.evals.runner import activated_fault

    calls: list[tuple[str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode() or "{}")
        calls.append((request.url.path, bool(payload.get("enabled"))))
        return httpx.Response(200, json={"ok": True})

    async def _run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            with pytest.raises(RuntimeError, match="boom"):
                async with activated_fault(
                    client,
                    base_url="http://t",
                    control_path="/internal/faults/pool-exhaustion",
                    token="tok",
                ):
                    raise RuntimeError("boom")

    asyncio.run(_run())
    assert calls[0] == ("/internal/faults/pool-exhaustion", True)
    assert calls[-1] == ("/internal/faults/pool-exhaustion", False)


def test_wait_for_incident_returns_none_without_alert() -> None:
    """Incident wait is bounded: an empty list yields None instead of hanging."""

    import asyncio
    from datetime import UTC, datetime

    import httpx

    from packages.evals.runner import _wait_for_incident

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "total": 0, "limit": 5, "offset": 0})

    async def _run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            result = await _wait_for_incident(
                client, "http://t", since=datetime.now(UTC), deadline_seconds=0
            )
            assert result is None

    asyncio.run(_run())


def test_wait_for_report_returns_present_report() -> None:
    """Report wait returns the parsed payload once the API serves it."""

    import asyncio

    import httpx

    from packages.evals.runner import _wait_for_report

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "RPT-001"})

    async def _run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            result = await _wait_for_report(client, "http://t", "INC-001", deadline_seconds=5)
            assert result == {"id": "RPT-001"}

    asyncio.run(_run())


def test_wait_for_incident_accepts_zulu_timestamps() -> None:
    """Incident wait parses Z-suffixed API timestamps instead of skipping them."""

    import asyncio
    from datetime import UTC, datetime

    import httpx

    from packages.evals.runner import _wait_for_incident

    item = {"id": "INC-AAAAAAAAAAAAAAAA", "created_at": "2030-01-01T00:00:00Z"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [item], "total": 1, "limit": 5, "offset": 0})

    async def _run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            result = await _wait_for_incident(
                client, "http://t", since=datetime(2000, 1, 1, tzinfo=UTC), deadline_seconds=0
            )
            assert result is not None
            assert result["id"] == "INC-AAAAAAAAAAAAAAAA"

    asyncio.run(_run())


def test_activated_fault_preserves_body_error_on_cleanup_failure() -> None:
    """A failed disable chains (not masks) the original scenario failure."""

    import asyncio

    import httpx

    from packages.evals.runner import activated_fault

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and b'"enabled":false' in request.content:
            return httpx.Response(500, json={"code": "boom"})
        return httpx.Response(200, json={"ok": True})

    async def _run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            with pytest.raises(RuntimeError, match="disable fault") as exc_info:
                async with activated_fault(
                    client,
                    base_url="http://t",
                    control_path="/internal/faults/pool-exhaustion",
                    token="tok",
                ):
                    raise RuntimeError("original-boom")
            assert isinstance(exc_info.value.__cause__, RuntimeError)
            assert "original-boom" in str(exc_info.value.__cause__)

    asyncio.run(_run())


def test_unowned_fault_raises_clear_error() -> None:
    """Accessing a fault outside the controller allowlist fails explicitly."""

    controller = _payment_controller(_settings())
    with pytest.raises(ValueError, match="not owned"):
        controller.state(FaultName.INVENTORY_TIMEOUT)
    with pytest.raises(ValueError, match="not owned"):
        controller.is_enabled(FaultName.INVENTORY_TIMEOUT)
