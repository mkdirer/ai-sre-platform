"""Shared bounded multi-fault control for demo services (Stage 09).

Each fault is explicit, allowlisted, always initializes disabled, and is
reversible. Control requires a development/test environment,
FAULT_INJECTION_ALLOWED=true, and a constant-time token match. Fault effects
are simulated bounded delays or deterministic error markers — never unbounded
load or host CPU burn. CPU saturation is a short bounded sleep plus a tiny
bounded hash loop, not a background burner.
"""

import asyncio
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import RLock

from opentelemetry import trace

from packages.config import Environment, Settings
from packages.models.faults import FaultName, FaultStateResponse
from packages.telemetry import TelemetryRuntime

AsyncSleeper = Callable[[float], Awaitable[None]]
StateCallback = Callable[[FaultName, bool], None]


class FaultControlDisabledError(Exception):
    """The process configuration forbids fault control."""


class FaultControlUnauthorizedError(Exception):
    """The supplied control credential is absent or invalid."""


@dataclass(frozen=True)
class MultiFaultConfiguration:
    """Validated immutable controls; runtime state is intentionally separate."""

    allowed: bool
    expected_token: str
    delays: dict[FaultName, float]
    service_name: str
    service_version: str
    environment: Environment

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        service_name: str,
        faults: tuple[FaultName, ...],
    ) -> "MultiFaultConfiguration":
        """Derive safe controls without accepting an initial enabled state."""

        environment_allowed = settings.environment in {
            Environment.DEVELOPMENT,
            Environment.TEST,
        }
        expected_token = settings.fault_control_token.get_secret_value()
        delays: dict[FaultName, float] = {}
        for fault in faults:
            delays[fault] = _delay_for_fault(settings, fault)
        return cls(
            allowed=(
                environment_allowed and settings.fault_injection_allowed and bool(expected_token)
            ),
            expected_token=expected_token,
            delays=delays,
            service_name=service_name,
            service_version=settings.service_version,
            environment=settings.environment,
        )


def _delay_for_fault(settings: Settings, fault: FaultName) -> float:
    if fault == FaultName.SLOW_DATABASE:
        return settings.slow_database_delay_seconds
    if fault == FaultName.POOL_EXHAUSTION:
        return settings.pool_exhaustion_delay_seconds
    if fault == FaultName.BAD_DEPLOYMENT:
        return settings.bad_deployment_delay_seconds
    if fault == FaultName.INVENTORY_TIMEOUT:
        return settings.inventory_timeout_delay_seconds
    if fault == FaultName.CPU_SATURATION:
        return settings.cpu_saturation_delay_seconds
    if fault == FaultName.HIGH_ERROR_RATE:
        return 0.0
    raise ValueError(f"unsupported fault: {fault}")


def _log_event(fault: FaultName) -> str:
    return f"fault.{fault.value}.injected"


class MultiFaultController:
    """Process-local allowlisted fault states behind one lock and auth guard."""

    def __init__(
        self,
        *,
        configuration: MultiFaultConfiguration,
        telemetry: TelemetryRuntime,
        sleeper: AsyncSleeper = asyncio.sleep,
        state_callback: StateCallback | None = None,
    ) -> None:
        self._configuration = configuration
        self._telemetry = telemetry
        self._sleeper = sleeper
        self._state_callback = state_callback
        self._lock = RLock()
        self._enabled: dict[FaultName, bool] = dict.fromkeys(configuration.delays, False)
        if state_callback is not None:
            for fault in configuration.delays:
                state_callback(fault, False)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        service_name: str,
        faults: tuple[FaultName, ...],
        telemetry: TelemetryRuntime,
        sleeper: AsyncSleeper = asyncio.sleep,
        state_callback: StateCallback | None = None,
    ) -> "MultiFaultController":
        """Build an always-off controller from validated settings."""

        return cls(
            configuration=MultiFaultConfiguration.from_settings(
                settings, service_name=service_name, faults=faults
            ),
            telemetry=telemetry,
            sleeper=sleeper,
            state_callback=state_callback,
        )

    def authorize(self, supplied_token: str | None) -> None:
        """Require both an allowed local environment and a constant-time token match."""

        if not self._configuration.allowed:
            raise FaultControlDisabledError
        try:
            matched = supplied_token is not None and secrets.compare_digest(
                supplied_token,
                self._configuration.expected_token,
            )
        except TypeError:
            # Non-ASCII header content is never a valid token; deny, don't 500.
            matched = False
        if not matched:
            raise FaultControlUnauthorizedError

    def state(self, fault: FaultName) -> FaultStateResponse:
        """Return an atomic snapshot without exposing the control token."""

        with self._lock:
            if fault not in self._enabled:
                raise ValueError(f"fault not owned by this controller: {fault}")
            enabled = self._enabled[fault]
        return FaultStateResponse(
            name=fault,
            enabled=enabled,
            allowed=self._configuration.allowed,
            delay_seconds=self._configuration.delays[fault],
            service=self._configuration.service_name,
            service_version=self._configuration.service_version,
            environment=self._configuration.environment.value,
        )

    def states(self) -> list[FaultStateResponse]:
        """Return snapshots for every owned fault in stable order."""

        return [self.state(fault) for fault in sorted(self._enabled, key=lambda f: f.value)]

    def set_enabled(self, fault: FaultName, enabled: bool) -> FaultStateResponse:
        """Set the desired state idempotently and publish one state-change event."""

        with self._lock:
            if fault not in self._enabled:
                raise ValueError(f"fault not owned by this controller: {fault}")
            changed = self._enabled[fault] != enabled
            self._enabled[fault] = enabled
        # Callback runs outside the lock so a failing observer cannot wedge
        # fault control; state is already committed at this point.
        if self._state_callback is not None:
            self._state_callback(fault, enabled)

        state = self.state(fault)
        self._annotate_current_span(state)
        self._telemetry.logger.info(
            f"fault.{fault.value}.state_changed",
            extra={
                "structured": {
                    "fault.name": state.name.value,
                    "fault.enabled": state.enabled,
                    "fault.changed": changed,
                    "fault.delay_seconds": state.delay_seconds,
                }
            },
        )
        return state

    def disable_all(self) -> None:
        """Reversibly disable every owned fault (cleanup path)."""

        for fault in list(self._enabled):
            self.set_enabled(fault, False)

    def is_enabled(self, fault: FaultName) -> bool:
        """Return the current flag without telemetry side effects."""

        with self._lock:
            if fault not in self._enabled:
                raise ValueError(f"fault not owned by this controller: {fault}")
            return self._enabled[fault]

    async def inject_delay(self, fault: FaultName) -> None:
        """Apply the bounded simulated delay for delay-type faults when enabled.

        The span annotation fires only for an actually injected fault: every
        request calls inject_delay for each delay-type fault in order, so an
        unconditional annotation here would overwrite another controller's
        (or this controller's enabled fault's) attributes with
        enabled=False — exactly what hid slow_database from Tempo proofs.
        """

        state = self.state(fault)
        if not state.enabled:
            return
        if fault == FaultName.HIGH_ERROR_RATE:
            return
        self._annotate_current_span(state)
        self._telemetry.logger.warning(
            _log_event(fault),
            extra={
                "structured": {
                    "fault.name": state.name.value,
                    "fault.enabled": True,
                    "fault.delay_seconds": state.delay_seconds,
                    "fault.location": "before_request_boundary",
                }
            },
        )
        if fault == FaultName.CPU_SATURATION:
            _bounded_cpu_work()
        if state.delay_seconds > 0:
            await self._sleeper(state.delay_seconds)

    def should_inject_error(self, fault: FaultName, key: str) -> bool:
        """Deterministically decide a simulated error (no randomness)."""

        if fault != FaultName.HIGH_ERROR_RATE:
            return False
        if not self.is_enabled(fault):
            return False
        digest = hashlib.sha256(key.encode()).hexdigest()
        return int(digest[:2], 16) % 2 == 0

    @staticmethod
    def _annotate_current_span(state: FaultStateResponse) -> None:
        span = trace.get_current_span()
        span.set_attribute("fault.name", state.name.value)
        span.set_attribute("fault.enabled", state.enabled)
        span.set_attribute("fault.delay_seconds", state.delay_seconds)
        span.set_attribute("service.version", state.service_version)
        span.set_attribute("deployment.environment", state.environment)


def _bounded_cpu_work(iterations: int = 2_000) -> None:
    """Tiny bounded hash loop standing in for CPU pressure (no host burn)."""

    digest = b"cpu-saturation-simulation"
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()
