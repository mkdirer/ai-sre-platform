"""Unit coverage for incident scoping, partial collection, IDs, and correlation."""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from packages.config import Settings
from packages.incidents.evidence_collection import EvidenceAdapters, EvidenceCollectionService
from packages.incidents.scoping import IncidentScopeError, scope_incident
from packages.incidents.timeline import correlate_timeline
from packages.models.deployments import DeploymentAtQuery, DeploymentWindowQuery
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceItem,
    EvidenceService,
    EvidenceSource,
    EvidenceType,
    EvidenceWindow,
    LogsAroundQuery,
    QueryTemplate,
    ServiceQuery,
)
from packages.persistence import stable_evidence_id
from packages.tools.http import AdapterUnavailableError

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
INCIDENT_ID = "INC-A1B2C3D4E5F60708"


@dataclass(frozen=True)
class _Incident:
    incident_id: str = INCIDENT_ID
    incident_title: str = "Payment latency"
    service: str = "payment-service"
    affected_services: tuple[str, ...] = ("payment-service",)
    started_at: datetime = NOW
    investigation_window_start: datetime = NOW - timedelta(minutes=10)
    investigation_window_end: datetime = NOW + timedelta(minutes=5)


class _Probe:
    def __init__(self) -> None:
        self.sources: set[EvidenceSource] = set()
        self.ready = asyncio.Event()

    async def enter(self, source: EvidenceSource) -> None:
        self.sources.add(source)
        if len(self.sources) == 4:
            self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=0.5)


def _draft(
    *,
    source: EvidenceSource,
    evidence_type: EvidenceType,
    template: QueryTemplate,
    service: EvidenceService,
    window: EvidenceWindow,
    limit_name: str,
    limit: int,
) -> EvidenceDraft:
    return EvidenceDraft(
        source=source,
        type=evidence_type,
        status=CollectionStatus.COLLECTED,
        observed_at=NOW,
        window=window,
        summary=f"Collected {template.value}",
        payload={"fixture": True},
        query_template=template,
        query_parameters={
            "service": service.value,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            limit_name: limit,
        },
        provenance={"adapter": source.value},
    )


class _Prometheus:
    def __init__(self, probe: _Probe) -> None:
        self.probe = probe

    async def _result(self, template: QueryTemplate, request: ServiceQuery) -> EvidenceDraft:
        await self.probe.enter(EvidenceSource.PROMETHEUS)
        if template == QueryTemplate.METRIC_SERVICE_CPU:
            raise AdapterUnavailableError("Prometheus unavailable")
        draft = _draft(
            source=EvidenceSource.PROMETHEUS,
            evidence_type=EvidenceType.METRIC,
            template=template,
            service=request.service,
            window=request.window,
            limit_name="series_limit",
            limit=request.limit,
        )
        return draft.model_copy(
            update={"query_parameters": draft.query_parameters | {"step_seconds": 5}}
        )

    async def get_service_latency(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._result(QueryTemplate.METRIC_SERVICE_LATENCY, request)

    async def get_service_error_rate(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._result(QueryTemplate.METRIC_SERVICE_ERROR_RATE, request)

    async def get_service_cpu(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._result(QueryTemplate.METRIC_SERVICE_CPU, request)

    async def get_service_memory(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._result(QueryTemplate.METRIC_SERVICE_MEMORY, request)

    async def get_db_pool_usage(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._result(QueryTemplate.METRIC_DB_POOL_USAGE, request)


class _Loki:
    def __init__(self, probe: _Probe) -> None:
        self.probe = probe

    async def get_errors(self, request: ServiceQuery) -> EvidenceDraft:
        await self.probe.enter(EvidenceSource.LOKI)
        return _draft(
            source=EvidenceSource.LOKI,
            evidence_type=EvidenceType.LOG,
            template=QueryTemplate.LOG_SERVICE_ERRORS,
            service=request.service,
            window=request.window,
            limit_name="entry_limit",
            limit=request.limit,
        )

    async def get_grouped_patterns(self, request: ServiceQuery) -> EvidenceDraft:
        await self.probe.enter(EvidenceSource.LOKI)
        return _draft(
            source=EvidenceSource.LOKI,
            evidence_type=EvidenceType.LOG,
            template=QueryTemplate.LOG_GROUPED_PATTERNS,
            service=request.service,
            window=request.window,
            limit_name="entry_limit",
            limit=request.limit,
        )

    async def get_logs_around(self, request: LogsAroundQuery) -> EvidenceDraft:
        await self.probe.enter(EvidenceSource.LOKI)
        await asyncio.sleep(1)
        raise AssertionError("source timeout should cancel this operation")


class _Tempo:
    def __init__(self, probe: _Probe) -> None:
        self.probe = probe

    async def _result(self, template: QueryTemplate, request: ServiceQuery) -> EvidenceDraft:
        await self.probe.enter(EvidenceSource.TEMPO)
        draft = _draft(
            source=EvidenceSource.TEMPO,
            evidence_type=EvidenceType.TRACE,
            template=template,
            service=request.service,
            window=request.window,
            limit_name="trace_limit",
            limit=request.limit,
        )
        return draft.model_copy(
            update={
                "query_parameters": draft.query_parameters
                | {"min_duration_ms": (500 if template == QueryTemplate.TRACE_SLOW_SERVICE else 50)}
            }
        )

    async def get_slow_service_traces(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._result(QueryTemplate.TRACE_SLOW_SERVICE, request)

    async def get_service_dependencies(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._result(QueryTemplate.TRACE_SERVICE_DEPENDENCIES, request)


class _Deployments:
    def __init__(self, probe: _Probe) -> None:
        self.probe = probe

    async def get_recent_deployments(self, request: DeploymentWindowQuery) -> EvidenceDraft:
        await self.probe.enter(EvidenceSource.DEPLOYMENT_STORE)
        return EvidenceDraft(
            source=EvidenceSource.DEPLOYMENT_STORE,
            type=EvidenceType.DEPLOYMENT,
            status=CollectionStatus.COLLECTED,
            observed_at=NOW,
            window=request.window,
            summary="Collected recent deployment metadata",
            payload={"fixture": True},
            query_template=QueryTemplate.DEPLOYMENT_RECENT,
            query_parameters={
                "service": request.service.value,
                "environment": request.environment.value,
                "window_start": request.window.start.isoformat(),
                "window_end": request.window.end.isoformat(),
                "deployment_limit": request.limit,
            },
            provenance={"adapter": "deployment_store"},
        )

    async def get_current_previous_version(self, request: DeploymentAtQuery) -> EvidenceDraft:
        await self.probe.enter(EvidenceSource.DEPLOYMENT_STORE)
        return EvidenceDraft(
            source=EvidenceSource.DEPLOYMENT_STORE,
            type=EvidenceType.DEPLOYMENT,
            status=CollectionStatus.COLLECTED,
            observed_at=NOW,
            window=request.window,
            summary="Collected current and previous deployment metadata",
            payload={"fixture": True},
            query_template=QueryTemplate.DEPLOYMENT_CURRENT_PREVIOUS,
            query_parameters={
                "service": request.service.value,
                "environment": request.environment.value,
                "at": request.at.isoformat(),
                "window_start": request.window.start.isoformat(),
                "window_end": request.window.end.isoformat(),
            },
            provenance={"adapter": "deployment_store"},
        )


class _EvidenceStore:
    def __init__(self) -> None:
        self.items: dict[str, EvidenceItem] = {}
        self.source_batches: list[EvidenceSource] = []

    async def persist_evidence(
        self,
        incident_id: str,
        drafts: list[EvidenceDraft],
    ) -> tuple[EvidenceItem, ...]:
        assert drafts
        self.source_batches.append(drafts[0].source)
        persisted: list[EvidenceItem] = []
        for draft in drafts:
            evidence_id = stable_evidence_id(
                incident_id=incident_id,
                source=draft.source,
                query_template=draft.query_template.value,
                query_parameters=draft.query_parameters,
            )
            item = EvidenceItem(
                **draft.model_dump(),
                id=evidence_id,
                incident_id=incident_id,
                payload_sha256="a" * 64,
                collected_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
            self.items[evidence_id] = item
            persisted.append(item)
        return tuple(persisted)


@pytest.mark.asyncio
async def test_collection_is_concurrent_bounded_and_persists_partial_results() -> None:
    """All sources start together; unavailable and timed-out operations do not erase success."""

    probe = _Probe()
    store = _EvidenceStore()
    settings = Settings(
        _env_file=None,
        environment="test",
        evidence_source_timeout_seconds=0.05,
    )
    service = EvidenceCollectionService(
        store=store,
        adapters=EvidenceAdapters(
            prometheus=_Prometheus(probe),  # type: ignore[arg-type]
            loki=_Loki(probe),  # type: ignore[arg-type]
            tempo=_Tempo(probe),  # type: ignore[arg-type]
            deployments=_Deployments(probe),  # type: ignore[arg-type]
        ),
        settings=settings,
        clock=lambda: NOW,
    )

    started = time.perf_counter()
    summaries = await service.collect(_Incident())
    duration = time.perf_counter() - started

    assert probe.sources == set(EvidenceSource)
    assert duration < 0.5
    assert len(store.items) == 12
    assert set(store.source_batches) == set(EvidenceSource)
    by_source = {summary.source: summary for summary in summaries}
    assert by_source[EvidenceSource.PROMETHEUS].unavailable == 1
    assert by_source[EvidenceSource.PROMETHEUS].collected == 4
    assert by_source[EvidenceSource.LOKI].timed_out == 1
    assert by_source[EvidenceSource.LOKI].collected == 2
    assert by_source[EvidenceSource.TEMPO].collected == 2
    assert by_source[EvidenceSource.DEPLOYMENT_STORE].collected == 2


def test_incident_scoping_rejects_unknown_services_and_unsafe_windows() -> None:
    settings = Settings(_env_file=None, environment="test")
    scope = scope_incident(_Incident(), settings, now=NOW)
    assert scope.services == (EvidenceService.PAYMENT,)
    assert scope.telemetry_window.start == NOW - timedelta(minutes=10)
    assert scope.deployment_window.start == NOW - timedelta(hours=2, minutes=10)

    with pytest.raises(IncidentScopeError, match="non-allowlisted"):
        scope_incident(
            _Incident(service='payment-service"} | malicious', affected_services=()),
            settings,
            now=NOW,
        )
    with pytest.raises(IncidentScopeError, match="maximum lookback"):
        scope_incident(
            _Incident(
                started_at=NOW - timedelta(hours=7),
                investigation_window_start=NOW - timedelta(hours=7, minutes=10),
                investigation_window_end=NOW - timedelta(hours=6, minutes=55),
            ),
            settings,
            now=NOW,
        )
    with pytest.raises(IncidentScopeError, match="configured bound"):
        scope_incident(
            _Incident(
                investigation_window_start=NOW - timedelta(hours=1),
                investigation_window_end=NOW + timedelta(minutes=5),
            ),
            settings,
            now=NOW,
        )


def _item(
    *,
    evidence_id: str,
    evidence_type: EvidenceType,
    source: EvidenceSource,
    observed_at: datetime,
    template: QueryTemplate,
    payload: dict[str, object],
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        incident_id=INCIDENT_ID,
        source=source,
        type=evidence_type,
        status=CollectionStatus.COLLECTED,
        observed_at=observed_at,
        window=EvidenceWindow(start=NOW - timedelta(hours=1), end=NOW + timedelta(minutes=5)),
        summary=f"Summary for {template.value}",
        payload=payload,
        query_template=template,
        query_parameters={"service": "payment-service"},
        provenance={"adapter": source.value},
        payload_sha256="b" * 64,
        collected_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def test_timeline_correlation_is_chronological_stable_and_evidence_linked() -> None:
    deployment = _item(
        evidence_id="EVD-111111111111111111111111",
        evidence_type=EvidenceType.DEPLOYMENT,
        source=EvidenceSource.DEPLOYMENT_STORE,
        observed_at=NOW,
        template=QueryTemplate.DEPLOYMENT_RECENT,
        payload={
            "deployments": [
                {
                    "id": "DEP-A1B2C3D4E5F607081122",
                    "service": "payment-service",
                    "version": "0.2.0",
                    "commit_sha": "a" * 40,
                    "deployed_at": (NOW - timedelta(minutes=5)).isoformat(),
                }
            ]
        },
    )
    metric = _item(
        evidence_id="EVD-222222222222222222222222",
        evidence_type=EvidenceType.METRIC,
        source=EvidenceSource.PROMETHEUS,
        observed_at=NOW + timedelta(seconds=1),
        template=QueryTemplate.METRIC_SERVICE_LATENCY,
        payload={"value": 2.5},
    )
    log = _item(
        evidence_id="EVD-333333333333333333333333",
        evidence_type=EvidenceType.LOG,
        source=EvidenceSource.LOKI,
        observed_at=NOW + timedelta(seconds=2),
        template=QueryTemplate.LOG_SERVICE_ERRORS,
        payload={
            "entries": [
                {
                    "timestamp": (NOW + timedelta(seconds=2)).isoformat(),
                    "severity": "ERROR",
                    "event": "database timeout",
                    "trace_id": "0123456789abcdef0123456789abcdef",
                }
            ]
        },
    )

    forward = correlate_timeline((log, metric, deployment), limit=100, offset=0)
    reverse = correlate_timeline((deployment, metric, log), limit=100, offset=0)

    assert [event.id for event in forward.items] == [event.id for event in reverse.items]
    assert [event.timestamp for event in forward.items] == sorted(
        event.timestamp for event in forward.items
    )
    assert [event.evidence_id for event in forward.items] == [
        deployment.id,
        metric.id,
        log.id,
    ]
    assert all(event.incident_id == INCIDENT_ID for event in forward.items)

    original_log_event_id = next(event.id for event in forward.items if event.evidence_id == log.id)
    late_entry = {
        "timestamp": (NOW + timedelta(seconds=1)).isoformat(),
        "severity": "INFO",
        "event": "late arriving context",
        "trace_id": "fedcba9876543210fedcba9876543210",
    }
    reordered_log = log.model_copy(
        update={"payload": {"entries": [late_entry, *reversed(log.payload["entries"])]}}
    )
    with_late_entry = correlate_timeline((reordered_log, metric, deployment), limit=100, offset=0)
    assert original_log_event_id in {
        event.id for event in with_late_entry.items if event.evidence_id == log.id
    }


def test_evidence_ids_are_stable_per_incident_template_scope() -> None:
    parameters = {
        "service": "payment-service",
        "window_start": NOW.isoformat(),
        "window_end": (NOW + timedelta(minutes=5)).isoformat(),
    }
    first = stable_evidence_id(
        incident_id=INCIDENT_ID,
        source=EvidenceSource.PROMETHEUS,
        query_template=QueryTemplate.METRIC_SERVICE_LATENCY.value,
        query_parameters=parameters,
    )
    replay = stable_evidence_id(
        incident_id=INCIDENT_ID,
        source=EvidenceSource.PROMETHEUS,
        query_template=QueryTemplate.METRIC_SERVICE_LATENCY.value,
        query_parameters=dict(reversed(list(parameters.items()))),
    )
    other_incident = stable_evidence_id(
        incident_id="INC-0000000000000000",
        source=EvidenceSource.PROMETHEUS,
        query_template=QueryTemplate.METRIC_SERVICE_LATENCY.value,
        query_parameters=parameters,
    )

    assert first == replay
    assert first != other_incident
