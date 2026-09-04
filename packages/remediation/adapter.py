"""Allowlisted demo rollback adapter; deterministic endpoint resolution only."""

from enum import StrEnum

import httpx

from packages.config import Settings
from packages.models.evidence import EvidenceService
from packages.models.faults import PAYMENT_FAULTS, FaultName
from packages.models.remediation import RollbackDeploymentParams


class AdapterOutcome(StrEnum):
    """Execution result taxonomy separating unknown from safe retry."""

    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    UNKNOWN = "unknown"
    FAILED = "failed"
    FORBIDDEN = "forbidden"


class AdapterResult:
    """Outcome plus redacted detail for audit (never URLs or secrets)."""

    def __init__(self, outcome: AdapterOutcome, *, detail: str = "") -> None:
        self.outcome = outcome
        self.detail = detail[:512]


def resolve_service_base_url(settings: Settings, service: EvidenceService) -> str:
    """Resolve the control-plane base URL from the allowlist, never from input."""

    if service == EvidenceService.PAYMENT:
        return str(settings.payment_service_url).rstrip("/")
    raise ValueError(f"service {service.value} has no registered control endpoint")


def _fault_path(fault: FaultName) -> str:
    return f"/internal/faults/{fault.value.replace('_', '-')}"


class PaymentServiceRollbackAdapter:
    """Disable listed payment faults via the guarded control API with read-back."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._timeout = settings.remediation_execution_timeout_seconds
        self._token = settings.fault_control_token.get_secret_value()

    async def _request(
        self, method: str, url: str, body: dict[str, object] | None
    ) -> httpx.Response:
        if self._client is not None:
            request = self._client.build_request(
                method, url, json=body, headers={"X-Fault-Control-Token": self._token}
            )
            return await self._client.send(request)
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
            return await client.request(
                method, url, json=body, headers={"X-Fault-Control-Token": self._token}
            )

    async def read_fault_enabled(self, base_url: str, fault: FaultName) -> bool | None:
        """Return current fault state, or None when the read itself fails."""

        try:
            response = await self._request("GET", f"{base_url}{_fault_path(fault)}", None)
        except httpx.HTTPError:
            return None
        if response.is_error:
            return None
        try:
            enabled = response.json().get("enabled", False)
        except ValueError:
            return None
        # Strict booleans only: truthy strings like "false" must never read
        # as enabled and mask a fault left on.
        return enabled if isinstance(enabled, bool) else None

    async def disable_faults(self, params: RollbackDeploymentParams) -> AdapterResult:
        """Disable every payment fault with read-back confirmation, never assume.

        The fault scope is fixed by the registry, not by model parameters:
        rollback returns the whole payment service to its known-good state.
        All effects are idempotent and reversible. A successful PUT is
        confirmed by a follow-up read; anything still enabled is unknown.
        """

        try:
            base_url = resolve_service_base_url(self._settings, params.service)
        except ValueError as error:
            return AdapterResult(AdapterOutcome.FORBIDDEN, detail=str(error))
        failures: list[str] = []
        unknown: list[str] = []
        changed = False
        for fault in PAYMENT_FAULTS:
            current = await self.read_fault_enabled(base_url, fault)
            if current is False:
                continue
            changed = True
            try:
                response = await self._request(
                    "PUT", f"{base_url}{_fault_path(fault)}", {"enabled": False}
                )
            except httpx.TimeoutException:
                # Sent-or-not is ambiguous: settle by reading actual state.
                confirmed = await self.read_fault_enabled(base_url, fault)
                if confirmed is False:
                    continue
                unknown.append(fault.value)
                continue
            except httpx.HTTPError as error:
                failures.append(f"{fault.value}: connection failed: {type(error).__name__}")
                continue
            if response.is_error and response.status_code not in (401, 403, 404):
                failures.append(f"{fault.value}: HTTP {response.status_code}")
            elif response.status_code in (401, 403, 404):
                return AdapterResult(
                    AdapterOutcome.FORBIDDEN,
                    detail=f"control rejected {fault.value}: HTTP {response.status_code}",
                )
        if unknown:
            return AdapterResult(
                AdapterOutcome.UNKNOWN,
                detail=f"unconfirmed faults settled by read-back: {','.join(unknown)}",
            )
        if failures:
            return AdapterResult(
                AdapterOutcome.FAILED, detail=f"safe to retry: {'; '.join(failures)}"
            )
        # Confirm successful writes: a 2xx that left the fault enabled (or an
        # unreadable state) is unknown, never success.
        unconfirmed = [
            fault.value
            for fault in PAYMENT_FAULTS
            if await self.read_fault_enabled(base_url, fault) is not False
        ]
        if unconfirmed:
            return AdapterResult(
                AdapterOutcome.UNKNOWN,
                detail=f"write succeeded but state unconfirmed: {','.join(unconfirmed)}",
            )
        if not changed:
            return AdapterResult(
                AdapterOutcome.ALREADY_APPLIED, detail="all faults already disabled"
            )
        return AdapterResult(AdapterOutcome.APPLIED, detail="listed faults confirmed disabled")
