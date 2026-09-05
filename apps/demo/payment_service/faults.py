"""Concurrency-safe, local-only control for the deterministic payment fault."""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import RLock

from opentelemetry import trace

from packages.config import Environment, Settings
from packages.models.faults import FaultStateResponse
from packages.telemetry import TelemetryRuntime

AsyncSleeper = Callable[[float], Awaitable[None]]
StateCallback = Callable[[bool], None]


class FaultControlDisabledError(Exception):
    """The process configuration forbids fault control."""


class FaultControlUnauthorizedError(Exception):
    """The supplied control credential is absent or invalid."""


@dataclass(frozen=True)
class FaultConfiguration:
    """Validated immutable controls; runtime state is intentionally separate."""

    allowed: bool
    expected_token: str
    delay_seconds: float
    service_version: str
    environment: Environment

    @classmethod
    def from_settings(cls, settings: Settings) -> "FaultConfiguration":
        """Derive safe controls without accepting an initial enabled state."""

        environment_allowed = settings.environment in {
            Environment.DEVELOPMENT,
            Environment.TEST,
        }
        expected_token = settings.fault_control_token.get_secret_value()
        return cls(
            allowed=(
                environment_allowed and settings.fault_injection_allowed and bool(expected_token)
            ),
            expected_token=expected_token,
            delay_seconds=settings.slow_database_delay_seconds,
            service_version=settings.service_version,
            environment=settings.environment,
        )


class SlowDatabaseFaultController:
    """Own explicit process-local fault state behind a lock and auth guard."""

    def __init__(
        self,
        *,
        configuration: FaultConfiguration,
        telemetry: TelemetryRuntime,
        sleeper: AsyncSleeper = asyncio.sleep,
        state_callback: StateCallback | None = None,
    ) -> None:
        self._configuration = configuration
        self._telemetry = telemetry
        self._sleeper = sleeper
        self._state_callback = state_callback
        self._lock = RLock()
        self._enabled = False
        if state_callback is not None:
            state_callback(False)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        telemetry: TelemetryRuntime,
        sleeper: AsyncSleeper = asyncio.sleep,
        state_callback: StateCallback | None = None,
    ) -> "SlowDatabaseFaultController":
        """Build an always-off controller from validated settings."""

        return cls(
            configuration=FaultConfiguration.from_settings(settings),
            telemetry=telemetry,
            sleeper=sleeper,
            state_callback=state_callback,
        )

    def authorize(self, supplied_token: str | None) -> None:
        """Require both an allowed local environment and a constant-time token match."""

        if not self._configuration.allowed:
            raise FaultControlDisabledError
        if supplied_token is None or not secrets.compare_digest(
            supplied_token,
            self._configuration.expected_token,
        ):
            raise FaultControlUnauthorizedError

    def state(self) -> FaultStateResponse:
        """Return an atomic snapshot without exposing the control token."""

        with self._lock:
            enabled = self._enabled
        return FaultStateResponse(
            enabled=enabled,
            allowed=self._configuration.allowed,
            delay_seconds=self._configuration.delay_seconds,
            service_version=self._configuration.service_version,
            environment=self._configuration.environment.value,
        )

    def set_enabled(self, enabled: bool) -> FaultStateResponse:
        """Set the desired state idempotently and publish one state-change event."""

        with self._lock:
            changed = self._enabled != enabled
            self._enabled = enabled
            if self._state_callback is not None:
                self._state_callback(enabled)

        state = self.state()
        self._annotate_current_span(state)
        self._telemetry.logger.info(
            "fault.slow_database.state_changed",
            extra={
                "structured": {
                    "fault.name": state.name,
                    "fault.enabled": state.enabled,
                    "fault.changed": changed,
                    "fault.delay_seconds": state.delay_seconds,
                }
            },
        )
        return state

    async def inject_before_database(self) -> None:
        """Apply a fixed delay immediately before persistence when enabled.

        The span annotation fires only when the fault is actually injected,
        mirroring MultiFaultController.inject_delay: an unconditional
        annotation here would overwrite another controller's enabled-fault
        attributes with enabled=False on the same span.
        """

        state = self.state()
        if not state.enabled:
            return
        self._annotate_current_span(state)

        self._telemetry.logger.warning(
            "fault.slow_database.injected",
            extra={
                "structured": {
                    "fault.name": state.name,
                    "fault.enabled": True,
                    "fault.delay_seconds": state.delay_seconds,
                    "fault.location": "before_payment_database",
                }
            },
        )
        await self._sleeper(state.delay_seconds)

    @staticmethod
    def _annotate_current_span(state: FaultStateResponse) -> None:
        span = trace.get_current_span()
        span.set_attribute("fault.name", state.name)
        span.set_attribute("fault.enabled", state.enabled)
        span.set_attribute("fault.delay_seconds", state.delay_seconds)
        span.set_attribute("service.version", state.service_version)
        span.set_attribute("deployment.environment", state.environment)
