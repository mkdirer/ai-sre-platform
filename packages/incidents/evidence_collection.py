"""Concurrent, source-isolated deterministic evidence collection service."""

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypeVar

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from packages.config import Settings
from packages.incidents.scoping import IncidentEvidenceScope, IncidentScopeInput, scope_incident
from packages.models.deployments import (
    DeploymentAtQuery,
    DeploymentEnvironment,
    DeploymentWindowQuery,
)
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceItem,
    EvidenceSource,
    EvidenceType,
    EvidenceWindow,
    LogsAroundQuery,
    QueryTemplate,
    ServiceQuery,
    SourceCollectionSummary,
)
from packages.telemetry import TelemetryRuntime, redact_text
from packages.tools.deployments import DeploymentAdapter
from packages.tools.http import (
    AdapterError,
    AdapterTimeoutError,
    AdapterUnavailableError,
)
from packages.tools.loki import LokiAdapter
from packages.tools.prometheus import PrometheusAdapter
from packages.tools.tempo import TempoAdapter

RequestT = TypeVar("RequestT")


class EvidenceWriter(Protocol):
    """Source-local durable write boundary required by the collector."""

    async def persist_evidence(
        self,
        incident_id: str,
        drafts: Sequence[EvidenceDraft],
    ) -> tuple[EvidenceItem, ...]: ...


@dataclass(frozen=True)
class EvidenceAdapters:
    """Complete deterministic adapter set required by Stage 3."""

    prometheus: PrometheusAdapter
    loki: LokiAdapter
    tempo: TempoAdapter
    deployments: DeploymentAdapter


@dataclass(frozen=True)
class _Operation:
    source: EvidenceSource
    template: QueryTemplate
    fallback: EvidenceDraft
    execute: Callable[[], Awaitable[EvidenceDraft]]


class EvidenceCollectionService:
    """Collect every source concurrently, persist failures explicitly, and never run AI."""

    def __init__(
        self,
        *,
        store: EvidenceWriter,
        adapters: EvidenceAdapters,
        settings: Settings,
        telemetry: TelemetryRuntime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._adapters = adapters
        self._settings = settings
        self._telemetry = telemetry
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tracer = trace.get_tracer(
            __name__,
            tracer_provider=telemetry.tracer_provider if telemetry is not None else None,
        )

    async def collect(
        self,
        incident: IncidentScopeInput,
    ) -> tuple[SourceCollectionSummary, ...]:
        """Execute a retry-safe complete source plan for canonical incident scope."""

        started = time.perf_counter()
        scope = scope_incident(incident, self._settings, now=self._clock())
        grouped: dict[EvidenceSource, list[_Operation]] = defaultdict(list)
        for operation in self._plan(scope):
            grouped[operation.source].append(operation)
        tasks = [
            asyncio.create_task(self._collect_source(scope.incident_id, source, operations))
            for source, operations in sorted(grouped.items(), key=lambda item: item[0].value)
        ]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures = [result for result in results if isinstance(result, BaseException)]
            if failures:
                raise failures[0]
            summaries = tuple(
                result for result in results if isinstance(result, SourceCollectionSummary)
            )
            outcome = (
                "partial"
                if any(
                    summary.unavailable + summary.failed + summary.timed_out > 0
                    for summary in summaries
                )
                else "success"
            )
            return summaries
        except BaseException:
            outcome = "failed"
            raise
        finally:
            duration = time.perf_counter() - started
            if self._telemetry is not None:
                self._telemetry.metrics.observe_evidence_collection(
                    outcome=outcome,
                    duration_seconds=duration,
                )
                self._telemetry.logger.info(
                    "evidence.collection.completed",
                    extra={
                        "structured": {
                            "collection.outcome": outcome,
                            "duration_ms": round(duration * 1_000, 3),
                        }
                    },
                )

    async def _collect_source(
        self,
        incident_id: str,
        source: EvidenceSource,
        operations: list[_Operation],
    ) -> SourceCollectionSummary:
        tasks = {
            asyncio.create_task(self._execute_operation(operation)): operation
            for operation in operations
        }
        done, pending = await asyncio.wait(
            tasks,
            timeout=self._settings.evidence_source_timeout_seconds,
        )
        drafts: list[EvidenceDraft] = []
        for task in done:
            operation = tasks[task]
            try:
                draft = task.result()
            except BaseException as error:
                draft = _failure_draft(operation.fallback, error)
            drafts.append(draft)
        for task in pending:
            task.cancel()
            operation = tasks[task]
            drafts.append(
                operation.fallback.with_status(
                    status=CollectionStatus.TIMED_OUT,
                    summary=(
                        f"{operation.template.value} exceeded the per-source collection deadline"
                    ),
                    error_type="SourceTimeout",
                    error_message="source collection deadline exceeded",
                )
            )
            self._observe_call(
                operation,
                status=CollectionStatus.TIMED_OUT,
                duration_seconds=self._settings.evidence_source_timeout_seconds,
                error_type="SourceTimeout",
            )
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        drafts.sort(key=lambda draft: (draft.query_template.value, str(draft.query_parameters)))
        persisted = await self._store.persist_evidence(incident_id, drafts)
        return _source_summary(source, persisted)

    async def _execute_operation(self, operation: _Operation) -> EvidenceDraft:
        started = time.perf_counter()
        status = CollectionStatus.FAILED
        error_type: str | None = None
        cancelled = False
        with self._tracer.start_as_current_span("evidence.adapter.collect") as span:
            span.set_attribute("evidence.source", operation.source.value)
            span.set_attribute("evidence.query_template", operation.template.value)
            try:
                draft = await operation.execute()
                if (
                    draft.source != operation.source
                    or draft.query_template != operation.template
                    or draft.query_parameters != operation.fallback.query_parameters
                ):
                    raise ValueError("adapter result did not match its fixed operation identity")
                status = draft.status
                return draft
            except asyncio.CancelledError:
                cancelled = True
                raise
            except BaseException as error:
                error_type = type(error).__name__
                span.set_status(Status(StatusCode.ERROR, error_type))
                raise
            finally:
                duration = time.perf_counter() - started
                if not cancelled:
                    self._observe_call(
                        operation,
                        status=_exception_status(error_type) if error_type else status,
                        duration_seconds=duration,
                        error_type=error_type,
                    )

    def _observe_call(
        self,
        operation: _Operation,
        *,
        status: CollectionStatus,
        duration_seconds: float,
        error_type: str | None,
    ) -> None:
        if self._telemetry is None:
            return
        self._telemetry.metrics.observe_adapter_call(
            source=operation.source.value,
            template=operation.template.value,
            outcome=status.value,
            duration_seconds=duration_seconds,
        )
        self._telemetry.logger.info(
            "evidence.adapter.completed",
            extra={
                "structured": {
                    "evidence.source": operation.source.value,
                    "evidence.query_template": operation.template.value,
                    "collection.status": status.value,
                    "duration_ms": round(duration_seconds * 1_000, 3),
                    "error.type": error_type,
                }
            },
        )

    def _plan(self, scope: IncidentEvidenceScope) -> tuple[_Operation, ...]:
        operations: list[_Operation] = []
        environment = DeploymentEnvironment(self._settings.environment.value)
        for service in scope.services:
            metric_request = ServiceQuery(
                service=service,
                window=scope.telemetry_window,
                limit=50,
            )
            metric_parameters = _service_parameters(metric_request, "series_limit") | {
                "step_seconds": self._settings.evidence_metric_step_seconds,
            }
            for template, method in (
                (
                    QueryTemplate.METRIC_SERVICE_LATENCY,
                    self._adapters.prometheus.get_service_latency,
                ),
                (
                    QueryTemplate.METRIC_SERVICE_ERROR_RATE,
                    self._adapters.prometheus.get_service_error_rate,
                ),
                (QueryTemplate.METRIC_SERVICE_CPU, self._adapters.prometheus.get_service_cpu),
                (QueryTemplate.METRIC_SERVICE_MEMORY, self._adapters.prometheus.get_service_memory),
                (QueryTemplate.METRIC_DB_POOL_USAGE, self._adapters.prometheus.get_db_pool_usage),
            ):
                operations.append(
                    _Operation(
                        source=EvidenceSource.PROMETHEUS,
                        template=template,
                        fallback=_fallback(
                            source=EvidenceSource.PROMETHEUS,
                            evidence_type=EvidenceType.METRIC,
                            template=template,
                            window=scope.telemetry_window,
                            parameters=metric_parameters,
                        ),
                        execute=_bind_operation(method, metric_request),
                    )
                )

            log_request = ServiceQuery(
                service=service,
                window=scope.telemetry_window,
                limit=self._settings.evidence_log_limit,
            )
            for template, method in (
                (QueryTemplate.LOG_SERVICE_ERRORS, self._adapters.loki.get_errors),
                (QueryTemplate.LOG_GROUPED_PATTERNS, self._adapters.loki.get_grouped_patterns),
            ):
                operations.append(
                    _Operation(
                        source=EvidenceSource.LOKI,
                        template=template,
                        fallback=_fallback(
                            source=EvidenceSource.LOKI,
                            evidence_type=EvidenceType.LOG,
                            template=template,
                            window=scope.telemetry_window,
                            parameters=_service_parameters(log_request, "entry_limit"),
                        ),
                        execute=_bind_operation(method, log_request),
                    )
                )
            around_request = LogsAroundQuery(
                service=service,
                window=scope.telemetry_window,
                limit=self._settings.evidence_log_limit,
                timestamp=scope.incident_timestamp,
                radius_seconds=120,
            )
            around_window = EvidenceWindow(
                start=max(
                    scope.telemetry_window.start,
                    scope.incident_timestamp - timedelta(seconds=120),
                ),
                end=min(
                    scope.telemetry_window.end,
                    scope.incident_timestamp + timedelta(seconds=120),
                ),
            )
            around_parameters = _service_parameters(around_request, "entry_limit") | {
                "timestamp": around_request.timestamp.isoformat(),
                "radius_seconds": around_request.radius_seconds,
            }
            operations.append(
                _Operation(
                    source=EvidenceSource.LOKI,
                    template=QueryTemplate.LOG_AROUND_TIMESTAMP,
                    fallback=_fallback(
                        source=EvidenceSource.LOKI,
                        evidence_type=EvidenceType.LOG,
                        template=QueryTemplate.LOG_AROUND_TIMESTAMP,
                        window=around_window,
                        parameters=around_parameters,
                    ),
                    execute=_bind_operation(
                        self._adapters.loki.get_logs_around,
                        around_request,
                    ),
                )
            )

            trace_request = ServiceQuery(
                service=service,
                window=scope.telemetry_window,
                limit=self._settings.evidence_trace_limit,
            )
            for template, method in (
                (QueryTemplate.TRACE_SLOW_SERVICE, self._adapters.tempo.get_slow_service_traces),
                (
                    QueryTemplate.TRACE_SERVICE_DEPENDENCIES,
                    self._adapters.tempo.get_service_dependencies,
                ),
            ):
                min_duration_ms = (
                    self._settings.evidence_slow_trace_threshold_ms
                    if template == QueryTemplate.TRACE_SLOW_SERVICE
                    else 50
                )
                operations.append(
                    _Operation(
                        source=EvidenceSource.TEMPO,
                        template=template,
                        fallback=_fallback(
                            source=EvidenceSource.TEMPO,
                            evidence_type=EvidenceType.TRACE,
                            template=template,
                            window=scope.telemetry_window,
                            parameters=_service_parameters(trace_request, "trace_limit")
                            | {"min_duration_ms": min_duration_ms},
                        ),
                        execute=_bind_operation(method, trace_request),
                    )
                )

            deployment_request = DeploymentWindowQuery(
                service=service,
                environment=environment,
                window=scope.deployment_window,
                limit=self._settings.evidence_deployment_limit,
            )
            deployment_parameters: dict[str, object] = {
                "service": service.value,
                "environment": environment.value,
                "window_start": scope.deployment_window.start.isoformat(),
                "window_end": scope.deployment_window.end.isoformat(),
                "deployment_limit": deployment_request.limit,
            }
            operations.append(
                _Operation(
                    source=EvidenceSource.DEPLOYMENT_STORE,
                    template=QueryTemplate.DEPLOYMENT_RECENT,
                    fallback=_fallback(
                        source=EvidenceSource.DEPLOYMENT_STORE,
                        evidence_type=EvidenceType.DEPLOYMENT,
                        template=QueryTemplate.DEPLOYMENT_RECENT,
                        window=scope.deployment_window,
                        parameters=deployment_parameters,
                    ),
                    execute=_bind_operation(
                        self._adapters.deployments.get_recent_deployments,
                        deployment_request,
                    ),
                )
            )
            at_request = DeploymentAtQuery(
                service=service,
                environment=environment,
                window=scope.deployment_window,
                at=scope.incident_timestamp,
            )
            at_parameters: dict[str, object] = {
                "service": service.value,
                "environment": environment.value,
                "at": scope.incident_timestamp.isoformat(),
                "window_start": scope.deployment_window.start.isoformat(),
                "window_end": scope.deployment_window.end.isoformat(),
            }
            operations.append(
                _Operation(
                    source=EvidenceSource.DEPLOYMENT_STORE,
                    template=QueryTemplate.DEPLOYMENT_CURRENT_PREVIOUS,
                    fallback=_fallback(
                        source=EvidenceSource.DEPLOYMENT_STORE,
                        evidence_type=EvidenceType.DEPLOYMENT,
                        template=QueryTemplate.DEPLOYMENT_CURRENT_PREVIOUS,
                        window=scope.deployment_window,
                        parameters=at_parameters,
                    ),
                    execute=_bind_operation(
                        self._adapters.deployments.get_current_previous_version,
                        at_request,
                    ),
                )
            )
        return tuple(operations)


def _fallback(
    *,
    source: EvidenceSource,
    evidence_type: EvidenceType,
    template: QueryTemplate,
    window: EvidenceWindow,
    parameters: dict[str, object],
) -> EvidenceDraft:
    return EvidenceDraft(
        source=source,
        type=evidence_type,
        status=CollectionStatus.EMPTY,
        observed_at=window.end,
        window=window,
        summary=f"No result was returned for {template.value}",
        payload={},
        query_template=template,
        query_parameters=parameters,
        provenance={"adapter": source.value},
    )


def _bind_operation(
    method: Callable[[RequestT], Awaitable[EvidenceDraft]],
    request: RequestT,
) -> Callable[[], Awaitable[EvidenceDraft]]:
    async def execute() -> EvidenceDraft:
        return await method(request)

    return execute


def _service_parameters(request: ServiceQuery, limit_name: str) -> dict[str, object]:
    return {
        "service": request.service.value,
        "window_start": request.window.start.isoformat(),
        "window_end": request.window.end.isoformat(),
        limit_name: request.limit,
    }


def _failure_draft(fallback: EvidenceDraft, error: BaseException) -> EvidenceDraft:
    status = _exception_status(type(error).__name__, error)
    message = redact_text(str(error))[:512] or "evidence collection failed"
    return fallback.with_status(
        status=status,
        summary=f"{fallback.query_template.value} collection {status.value}",
        error_type=type(error).__name__[:128],
        error_message=message,
    )


def _exception_status(
    error_type: str | None,
    error: BaseException | None = None,
) -> CollectionStatus:
    if isinstance(error, AdapterTimeoutError) or error_type in {
        "AdapterTimeoutError",
        "TimeoutError",
    }:
        return CollectionStatus.TIMED_OUT
    if isinstance(error, AdapterUnavailableError) or error_type == "AdapterUnavailableError":
        return CollectionStatus.UNAVAILABLE
    if isinstance(error, AdapterError):
        return CollectionStatus.FAILED
    return CollectionStatus.FAILED


def _source_summary(
    source: EvidenceSource,
    evidence: tuple[EvidenceItem, ...],
) -> SourceCollectionSummary:
    counts = {status: 0 for status in CollectionStatus}
    for item in evidence:
        counts[item.status] += 1
    return SourceCollectionSummary(
        source=source,
        collected=counts[CollectionStatus.COLLECTED],
        empty=counts[CollectionStatus.EMPTY],
        unavailable=counts[CollectionStatus.UNAVAILABLE],
        failed=counts[CollectionStatus.FAILED],
        timed_out=counts[CollectionStatus.TIMED_OUT],
    )
