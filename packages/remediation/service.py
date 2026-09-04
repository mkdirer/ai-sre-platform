"""Approval-gated remediation execution: validate, execute, verify, resolve."""

import asyncio
import math
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from packages.config import Settings
from packages.models.deployments import (
    DeploymentEnvironment,
    DeploymentRecord,
    DeploymentRegistration,
)
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceService,
    EvidenceWindow,
    ServiceQuery,
)
from packages.models.remediation import (
    ExecutionStatus,
    ForbiddenRemediationAction,
    RecommendationContext,
    RemediationExecution,
    RollbackDeploymentParams,
)
from packages.persistence.evidence_store import DeploymentConflict, EvidenceStoreUnavailable
from packages.persistence.remediation_store import RemediationConflict, RemediationNotFound
from packages.remediation.adapter import AdapterOutcome, AdapterResult
from packages.remediation.registry import validate_rollback_params
from packages.tools.http import AdapterError
from packages.tools.prometheus import PrometheusAdapter


class ExecutionOutcome(StrEnum):
    """Worker-visible terminal result of one execution run."""

    COMPLETED = "completed"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"
    STOPPED = "stopped"
    SKIPPED_IDEMPOTENT = "skipped_idempotent"


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome plus audit-safe detail for the task record."""

    outcome: ExecutionOutcome
    execution: RemediationExecution
    detail: str = ""


class ExecutionStore(Protocol):
    """Persistence boundary required by the remediation service."""

    async def mark_executing(self, execution_id: str, *, actor: str) -> RemediationExecution: ...
    async def mark_verifying(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution: ...
    async def mark_completed(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution: ...
    async def mark_ambiguous(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution: ...
    async def mark_failed(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution: ...
    async def get_execution(self, execution_id: str) -> RemediationExecution | None: ...


class ContextReader(Protocol):
    """Read the approved recommendation behind an execution."""

    async def recommendation_for(self, execution_id: str) -> RecommendationContext: ...


class DeploymentHistory(Protocol):
    """Deployment registry reads and rollback-record writes."""

    async def current_previous_deployments(
        self,
        *,
        service: EvidenceService,
        environment: DeploymentEnvironment,
        at: datetime,
    ) -> tuple[DeploymentRecord, ...]: ...
    async def register_deployment(self, registration: DeploymentRegistration) -> object: ...


class RollbackAdapter(Protocol):
    """Allowlisted rollback execution with a read-back outcome taxonomy."""

    async def disable_faults(self, params: RollbackDeploymentParams) -> AdapterResult: ...


LatencyProbe = Callable[[EvidenceService], Awaitable[float | None]]


class RemediationExecutionService:
    """Run one claimed execution from adapter run through verified recovery."""

    def __init__(
        self,
        store: ExecutionStore,
        contexts: ContextReader,
        deployments: DeploymentHistory,
        adapter: RollbackAdapter,
        settings: Settings,
        *,
        latency_probe: LatencyProbe | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._contexts = contexts
        self._deployments = deployments
        self._adapter = adapter
        self._settings = settings
        self._latency_probe = latency_probe
        self._sleeper = sleeper

    async def execute(self, *, execution_id: str, actor: str) -> ExecutionResult:
        """Execute once; redelivery of a non-pending execution is an idempotent no-op."""

        current = await self._store.get_execution(execution_id)
        if current is None:
            raise RemediationNotFound(f"execution {execution_id} not found")
        if current.status != ExecutionStatus.PENDING:
            return ExecutionResult(
                ExecutionOutcome.SKIPPED_IDEMPOTENT, current, "execution already claimed"
            )
        await self._store.mark_executing(execution_id, actor=actor)
        try:
            return await self._execute_claimed(execution_id, actor, current)
        except (RemediationConflict, RemediationNotFound):
            raise
        except Exception as error:
            # Only a process crash escapes this: best-effort terminal mark so
            # the execution never strands in executing without an outcome.
            with suppress(Exception):
                await self._store.mark_failed(
                    execution_id,
                    actor=actor,
                    details={
                        "error": type(error).__name__,
                        "stage": "unexpected",
                    },
                )
            raise

    async def _execute_claimed(
        self, execution_id: str, actor: str, current: RemediationExecution
    ) -> ExecutionResult:
        """Validate, adapt, and verify one claimed execution."""

        context = await self._contexts.recommendation_for(execution_id)
        try:
            params = validate_rollback_params(
                action=context.action_type,
                target=context.target,
                parameters=context.parameters,
            )
        except ForbiddenRemediationAction as error:
            failed = await self._store.mark_failed(
                execution_id, actor=actor, details={"error": error.code, "stage": "validate"}
            )
            return ExecutionResult(ExecutionOutcome.FAILED, failed, error.code)
        versions = await self._current_previous(current, params)
        if versions is None:
            failed = await self._store.mark_failed(
                execution_id,
                actor=actor,
                details={"error": "stale_state", "stage": "validate"},
            )
            return ExecutionResult(ExecutionOutcome.FAILED, failed, "stale deployment state")
        outcome = await self._run_adapter(execution_id, actor, params)
        if outcome is not None:
            return outcome
        registration_gap = await self._register_rollback_record(params)
        details: dict[str, object] = {"stage": "adapter"}
        if registration_gap is not None:
            details["registration_gap"] = registration_gap
        verifying = await self._store.mark_verifying(execution_id, actor=actor, details=details)
        return await self._verify_loop(verifying, actor, params)

    async def _current_previous(
        self, execution: RemediationExecution, params: RollbackDeploymentParams
    ) -> tuple[DeploymentRecord, DeploymentRecord] | None:
        try:
            records = await self._deployments.current_previous_deployments(
                service=params.service,
                environment=DeploymentEnvironment(self._settings.environment.value),
                at=datetime.now(UTC),
            )
        except (EvidenceStoreUnavailable, ValueError):
            return None
        if len(records) < 2:
            return None
        current, previous = records[0], records[1]
        expected = execution.result.get("expected_service_version")
        if (
            previous.id != params.deployment_id
            or previous.version != params.version
            or current.version == params.version
            or (expected is not None and current.version != expected)
        ):
            return None
        return current, previous

    async def _run_adapter(
        self, execution_id: str, actor: str, params: RollbackDeploymentParams
    ) -> ExecutionResult | None:
        attempts = self._settings.remediation_execution_max_attempts
        last: AdapterResult | None = None
        for _ in range(attempts):
            result = await self._adapter.disable_faults(params)
            last = result
            if result.outcome in (AdapterOutcome.APPLIED, AdapterOutcome.ALREADY_APPLIED):
                return None
            if result.outcome == AdapterOutcome.FORBIDDEN:
                failed = await self._store.mark_failed(
                    execution_id,
                    actor=actor,
                    details={"error": "forbidden_action", "stage": "adapter"},
                )
                return ExecutionResult(ExecutionOutcome.FAILED, failed, result.detail)
        detail = last.detail if last is not None else "adapter produced no result"
        failed = await self._store.mark_failed(
            execution_id,
            actor=actor,
            details={"error": "adapter_failed", "stage": "adapter", "detail": detail},
        )
        return ExecutionResult(ExecutionOutcome.FAILED, failed, detail)

    async def _register_rollback_record(self, params: RollbackDeploymentParams) -> str | None:
        try:
            records = await self._deployments.current_previous_deployments(
                service=params.service,
                environment=DeploymentEnvironment(self._settings.environment.value),
                at=datetime.now(UTC),
            )
            commit = records[1].commit_sha if len(records) > 1 else "0" * 40
            await self._deployments.register_deployment(
                DeploymentRegistration(
                    service=params.service,
                    environment=DeploymentEnvironment(self._settings.environment.value),
                    version=params.version,
                    deployed_at=datetime.now(UTC),
                    commit_sha=commit,
                    changed_files=["rollback/payment"],
                    metadata={"role": "rollback", "deployment_id": params.deployment_id},
                )
            )
        except (EvidenceStoreUnavailable, DeploymentConflict, ValueError) as error:
            return f"rollback record gap: {type(error).__name__}"
        return None

    async def _verify_loop(
        self, execution: RemediationExecution, actor: str, params: RollbackDeploymentParams
    ) -> ExecutionResult:
        deadline = self._settings.remediation_verification_window_seconds
        poll = self._settings.remediation_verification_poll_seconds
        threshold = self._settings.remediation_recovery_p95_threshold_seconds
        required = self._settings.remediation_required_healthy_polls
        started = datetime.now(UTC)
        healthy = 0
        samples: list[float] = []
        gaps: list[str] = []
        while (datetime.now(UTC) - started).total_seconds() < deadline:
            live = await self._store.get_execution(execution.id)
            if live is None:
                return ExecutionResult(
                    ExecutionOutcome.STOPPED, execution, "execution record vanished"
                )
            if live.stop_requested or live.status == ExecutionStatus.STOPPED:
                return ExecutionResult(ExecutionOutcome.STOPPED, live, "stop requested")
            if live.status != ExecutionStatus.VERIFYING:
                return ExecutionResult(
                    ExecutionOutcome.SKIPPED_IDEMPOTENT,
                    live,
                    "superseded by newer execution state",
                )
            sample = await self._sample(params.service)
            if sample is None:
                gaps.append("telemetry unavailable during verification")
            else:
                samples.append(sample)
                healthy = healthy + 1 if sample <= threshold else 0
            if healthy >= required:
                completed = await self._store.mark_completed(
                    execution.id,
                    actor=actor,
                    details={
                        "stage": "verify",
                        "p95_threshold_seconds": threshold,
                        "healthy_polls": healthy,
                        "samples": samples[-required:],
                    },
                )
                return ExecutionResult(ExecutionOutcome.COMPLETED, completed, "recovery verified")
            await self._sleeper(poll)
        if not samples:
            gaps.append("no telemetry samples observed in the verification window")
        else:
            gaps.append(
                f"p95 stayed above {threshold}s; {len(samples)} samples, best {min(samples):.3f}s"
            )
        ambiguous = await self._store.mark_ambiguous(
            execution.id, actor=actor, details={"stage": "verify", "gaps": gaps}
        )
        return ExecutionResult(ExecutionOutcome.AMBIGUOUS, ambiguous, "; ".join(gaps))

    async def _sample(self, service: EvidenceService) -> float | None:
        if self._latency_probe is None:
            return None
        return await self._latency_probe(service)


class PrometheusRecoveryProbe:
    """Deterministic p95 probe over the allowlisted latency template."""

    def __init__(self, adapter: PrometheusAdapter) -> None:
        self._adapter = adapter

    async def current_p95(self, service: EvidenceService) -> float | None:
        """Return the latest p95 sample, or None when unavailable."""

        end = datetime.now(UTC)
        try:
            draft: EvidenceDraft = await self._adapter.get_service_latency(
                ServiceQuery(
                    service=service,
                    window=EvidenceWindow(start=end - timedelta(minutes=5), end=end),
                )
            )
        except AdapterError:
            return None
        if draft.status != CollectionStatus.COLLECTED:
            return None
        latest: tuple[datetime, float] | None = None
        series = draft.payload.get("series")
        if not isinstance(series, list):
            return None
        for entry in series:
            if not isinstance(entry, dict):
                continue
            samples = entry.get("samples")
            if not isinstance(samples, list):
                continue
            for sample in samples:
                if not isinstance(sample, dict):
                    continue
                value = sample.get("value")
                if isinstance(value, bool) or not isinstance(value, int | float):
                    continue
                if not math.isfinite(value):
                    continue
                try:
                    observed = datetime.fromisoformat(str(sample.get("timestamp", "")))
                except ValueError:
                    continue
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=UTC)
                if latest is None or observed > latest[0]:
                    latest = (observed, float(value))
        return latest[1] if latest is not None else None
