"""Unit coverage for safe, deterministic slow-database fault control."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from apps.demo.payment_service.faults import (
    FaultControlDisabledError,
    FaultControlUnauthorizedError,
    SlowDatabaseFaultController,
)
from packages.config import Settings
from packages.telemetry import TelemetryRuntime
from packages.telemetry.metrics import HttpMetrics


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "telemetry_enabled": False,
        "fault_injection_allowed": True,
        "fault_control_token": "unit-test-control-token",
        "slow_database_delay_seconds": 2.5,
    }
    values.update(overrides)
    return Settings(**values)


def _controller(
    settings: Settings,
    *,
    sleeper: object | None = None,
) -> SlowDatabaseFaultController:
    runtime = TelemetryRuntime.create(service_name="payment-service", settings=settings)
    if sleeper is None:
        return SlowDatabaseFaultController.from_settings(settings, telemetry=runtime)
    return SlowDatabaseFaultController.from_settings(
        settings,
        telemetry=runtime,
        sleeper=sleeper,  # type: ignore[arg-type]
    )


def test_fault_is_off_and_denied_by_safe_defaults() -> None:
    """Missing opt-in and token cannot accidentally expose or enable the fault."""

    settings = Settings(_env_file=None, telemetry_enabled=False)
    controller = _controller(settings)

    assert controller.state().enabled is False
    assert controller.state().allowed is False
    with pytest.raises(FaultControlDisabledError):
        controller.authorize(None)


def test_fault_control_token_is_secret_safe() -> None:
    """The local control credential is absent from normal settings representations."""

    token = "unit-test-control-token"
    settings = _settings(fault_control_token=token)

    assert settings.fault_control_token.get_secret_value() == token
    assert token not in repr(settings)
    assert token not in str(settings.model_dump())


@pytest.mark.parametrize(
    ("environment", "allowed"),
    [("development", True), ("test", True), ("production", False)],
)
def test_fault_control_is_environment_guarded(environment: str, allowed: bool) -> None:
    """Even an explicit opt-in never permits this demo control in production."""

    controller = _controller(_settings(environment=environment))

    assert controller.state().allowed is allowed
    if allowed:
        controller.authorize("unit-test-control-token")
    else:
        with pytest.raises(FaultControlDisabledError):
            controller.authorize("unit-test-control-token")


def test_fault_control_requires_exact_token() -> None:
    """Missing and incorrect credentials fail without changing state."""

    controller = _controller(_settings())

    with pytest.raises(FaultControlUnauthorizedError):
        controller.authorize(None)
    with pytest.raises(FaultControlUnauthorizedError):
        controller.authorize("incorrect")
    assert controller.state().enabled is False


def test_invalid_fault_environment_values_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid boolean and delay values stop settings validation instead of enabling a fault."""

    monkeypatch.setenv("FAULT_INJECTION_ALLOWED", "perhaps")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.delenv("FAULT_INJECTION_ALLOWED")
    monkeypatch.setenv("SLOW_DATABASE_DELAY_SECONDS", "1.9")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.asyncio
async def test_enabled_fault_uses_one_fixed_delay_and_publishes_state() -> None:
    """Injection is deterministic and the state metric changes in lockstep."""

    delays: list[float] = []
    states: list[bool] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    settings = _settings()
    runtime = TelemetryRuntime.create(service_name="payment-service", settings=settings)
    controller = SlowDatabaseFaultController.from_settings(
        settings,
        telemetry=runtime,
        sleeper=sleeper,
        state_callback=states.append,
    )

    controller.authorize("unit-test-control-token")
    controller.set_enabled(True)
    await controller.inject_before_database()
    controller.set_enabled(False)
    await controller.inject_before_database()

    assert delays == [2.5]
    assert states == [False, True, False]


def test_fault_state_changes_are_thread_safe_and_reversible() -> None:
    """Concurrent explicit writes leave a valid state and can always be disabled."""

    controller = _controller(_settings())
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(controller.set_enabled, [True, False] * 50))

    final_state = controller.set_enabled(False)
    assert final_state.enabled is False


def test_fault_gauge_has_fixed_labels_and_reflects_changes() -> None:
    """The visible state metric changes without accepting arbitrary label values."""

    metrics = HttpMetrics("payment-service")
    assert (
        'demo_fault_enabled{fault="slow_database",service="payment-service"} 0.0'
        in metrics.render().decode()
    )

    metrics.set_slow_database_fault(True)
    exposition = metrics.render().decode()
    assert 'demo_fault_enabled{fault="slow_database",service="payment-service"} 1.0' in exposition
    assert "request_id" not in exposition
