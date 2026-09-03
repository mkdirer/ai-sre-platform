"""Unit coverage for fixed telemetry and deployment evidence adapters."""

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from packages.config import Settings
from packages.models.deployments import (
    DeploymentAtQuery,
    DeploymentCommitQuery,
    DeploymentEnvironment,
    DeploymentRecord,
    DeploymentWindowQuery,
)
from packages.models.evidence import (
    CollectionStatus,
    EvidenceService,
    EvidenceSource,
    EvidenceWindow,
    LogsAroundQuery,
    QueryTemplate,
    ServiceQuery,
    TraceByIdQuery,
)
from packages.persistence import stable_evidence_id
from packages.tools.deployments import DeploymentAdapter, DeploymentClient
from packages.tools.http import (
    AdapterResponseError,
    AdapterTimeoutError,
    AdapterUnavailableError,
)
from packages.tools.loki import LokiAdapter, LokiClient
from packages.tools.prometheus import PrometheusAdapter, PrometheusClient
from packages.tools.tempo import TempoAdapter, TempoClient

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
WINDOW = EvidenceWindow(start=NOW - timedelta(minutes=10), end=NOW + timedelta(minutes=5))
TRACE_ID = "0123456789abcdef0123456789abcdef"


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.chunks_yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.chunks_yielded += 1
            yield chunk

    async def aclose(self) -> None:
        return None


def _streaming_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    def streaming_handler(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        if not response.is_stream_consumed:
            return response
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_ChunkedStream([response.content]),
        )

    return httpx.MockTransport(streaming_handler)


def _settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "evidence_http_max_attempts": 1,
        "evidence_http_retry_backoff_seconds": 0,
    }
    values.update(updates)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_prometheus_adapter_uses_fixed_template_and_normalizes_samples() -> None:
    """The public metric method cannot supply PromQL and persists no rendered query."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"service": "payment-service"},
                            "values": [
                                [NOW.timestamp(), "1.25"],
                                [(NOW + timedelta(seconds=5)).timestamp(), "2.5"],
                            ],
                        }
                    ],
                },
            },
        )

    client = PrometheusClient(_settings(), transport=_streaming_transport(handler))
    try:
        evidence = await PrometheusAdapter(client).get_service_latency(
            ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW, limit=5)
        )
    finally:
        await client.close()

    assert evidence.status == CollectionStatus.COLLECTED
    assert "maximum 2.5 seconds" in evidence.summary
    assert "query" not in evidence.model_dump()
    assert requests[0].url.path == "/api/v1/query_range"
    rendered = requests[0].url.params["query"]
    assert 'service="payment-service"' in rendered
    assert "histogram_quantile" in rendered


@pytest.mark.asyncio
async def test_prometheus_distinguishes_no_data_timeout_and_malformed_response() -> None:
    """Empty data is valid while timeout and malformed payloads retain distinct errors."""

    request = ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW)

    def empty_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "success", "data": {"resultType": "matrix", "result": []}},
        )

    empty_client = PrometheusClient(_settings(), transport=_streaming_transport(empty_handler))
    try:
        empty = await PrometheusAdapter(empty_client).get_service_cpu(request)
    finally:
        await empty_client.close()
    assert empty.status == CollectionStatus.EMPTY

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fixture timeout", request=request)

    timeout_client = PrometheusClient(
        _settings(evidence_http_timeout_seconds=0.01),
        transport=_streaming_transport(timeout_handler),
    )
    try:
        with pytest.raises(AdapterTimeoutError):
            await PrometheusAdapter(timeout_client).get_service_memory(request)
    finally:
        await timeout_client.close()

    def malformed_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"result": "bad"}})

    malformed_client = PrometheusClient(
        _settings(), transport=_streaming_transport(malformed_handler)
    )
    try:
        with pytest.raises(AdapterResponseError):
            await PrometheusAdapter(malformed_client).get_service_error_rate(request)
    finally:
        await malformed_client.close()


@pytest.mark.asyncio
async def test_loki_adapter_redacts_and_groups_untrusted_messages_locally() -> None:
    """Grouping is deterministic local code and obvious credentials never survive payloads."""

    seen_queries: list[str] = []
    lines = [
        json.dumps(
            {
                "severity": "ERROR",
                "event": "database timeout 123",
                "trace_id": TRACE_ID,
                "span_id": "0123456789abcdef",
                "attributes": {"password": "never-store"},
            }
        ),
        json.dumps(
            {
                "severity": "ERROR",
                "event": "database timeout 456",
                "trace_id": TRACE_ID,
            }
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.params["query"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [
                        {
                            "stream": {"service_name": "payment-service"},
                            "values": [
                                [str(int(NOW.timestamp() * 1_000_000_000)), lines[0]],
                                [
                                    str(
                                        int(
                                            (NOW + timedelta(seconds=1)).timestamp() * 1_000_000_000
                                        )
                                    ),
                                    lines[1],
                                ],
                            ],
                        }
                    ],
                },
            },
        )

    client = LokiClient(_settings(), transport=_streaming_transport(handler))
    adapter = LokiAdapter(client)
    request = ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW, limit=10)
    try:
        errors = await adapter.get_errors(request)
        patterns = await adapter.get_grouped_patterns(request)
        around = await adapter.get_logs_around(
            LogsAroundQuery(
                service=EvidenceService.PAYMENT,
                window=WINDOW,
                timestamp=NOW,
                radius_seconds=30,
                limit=10,
            )
        )
    finally:
        await client.close()

    assert errors.status == patterns.status == around.status == CollectionStatus.COLLECTED
    assert "never-store" not in json.dumps(errors.payload)
    assert "[REDACTED]" in json.dumps(errors.payload)
    pattern_payload = patterns.payload["patterns"]
    assert isinstance(pattern_payload, list)
    assert pattern_payload[0]["pattern"] == "database timeout <number>"
    assert pattern_payload[0]["count"] == 2
    assert seen_queries[0] == (
        '{service_name="payment-service"} | json | severity=~"ERROR|CRITICAL"'
    )
    assert seen_queries[1:] == [
        '{service_name="payment-service"}',
        '{service_name="payment-service"}',
    ]


@pytest.mark.asyncio
async def test_loki_rejects_malformed_stream_values() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [{"stream": {}, "values": [["missing-line"]]}],
                },
            },
        )

    client = LokiClient(_settings(), transport=_streaming_transport(handler))
    try:
        with pytest.raises(AdapterResponseError):
            await LokiAdapter(client).get_errors(
                ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW)
            )
    finally:
        await client.close()


def _tempo_trace_payload() -> dict[str, object]:
    start = int(NOW.timestamp() * 1_000_000_000)
    return {
        "batches": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "gateway"}}]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "ASNFZ4mrze8BI0VniavN7w==",
                                "spanId": "ERERERERERE=",
                                "name": "POST /checkout",
                                "startTimeUnixNano": str(start),
                                "endTimeUnixNano": str(start + 3_000_000_000),
                            }
                        ]
                    }
                ],
            },
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "payment-service"},
                        }
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "ASNFZ4mrze8BI0VniavN7w==",
                                "spanId": "IiIiIiIiIiI=",
                                "parentSpanId": "ERERERERERE=",
                                "name": "POST /payments",
                                "startTimeUnixNano": str(start + 10_000_000),
                                "endTimeUnixNano": str(start + 2_510_000_000),
                                "attributes": [
                                    {
                                        "key": "fault.enabled",
                                        "value": {"boolValue": True},
                                    }
                                ],
                            }
                        ]
                    }
                ],
            },
        ]
    }


@pytest.mark.asyncio
async def test_tempo_adapter_returns_slow_trace_lookup_and_dependency_edges() -> None:
    """Tempo search parameters are fixed and parent-child services form dependency evidence."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/search":
            return httpx.Response(
                200,
                json={
                    "traces": [
                        {
                            "traceID": TRACE_ID,
                            "rootServiceName": "gateway",
                            "rootTraceName": "POST /checkout",
                            "startTimeUnixNano": str(int(NOW.timestamp() * 1_000_000_000)),
                            "durationMs": 3000,
                        }
                    ],
                    "metrics": {"inspectedTraces": 1},
                },
            )
        if request.url.path == f"/api/traces/{TRACE_ID}":
            return httpx.Response(200, json=_tempo_trace_payload())
        return httpx.Response(404)

    client = TempoClient(_settings(), transport=_streaming_transport(handler))
    adapter = TempoAdapter(client)
    request = ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW, limit=5)
    try:
        slow = await adapter.get_slow_service_traces(request)
        dependencies = await adapter.get_service_dependencies(request)
        trace = await adapter.get_trace_by_id(
            TraceByIdQuery(
                service=EvidenceService.PAYMENT,
                window=WINDOW,
                trace_id=TRACE_ID,
            )
        )
    finally:
        await client.close()

    assert slow.status == dependencies.status == trace.status == CollectionStatus.COLLECTED
    edges = dependencies.payload["dependencies"]
    assert isinstance(edges, list)
    assert edges[0]["parent_service"] == "gateway"
    assert edges[0]["child_service"] == "payment-service"
    search_requests = [request for request in requests if request.url.path == "/api/search"]
    assert all(
        request.url.params["tags"] == "service.name=payment-service" for request in search_requests
    )
    assert search_requests[0].url.params["minDuration"] == "500ms"
    assert search_requests[1].url.params["minDuration"] == "50ms"


@pytest.mark.asyncio
async def test_tempo_404_is_empty_and_malformed_search_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/traces/"):
            return httpx.Response(404)
        return httpx.Response(200, json={"traces": [{"traceID": "invalid"}]})

    client = TempoClient(_settings(), transport=_streaming_transport(handler))
    adapter = TempoAdapter(client)
    try:
        empty = await adapter.get_trace_by_id(
            TraceByIdQuery(
                service=EvidenceService.PAYMENT,
                window=WINDOW,
                trace_id=TRACE_ID,
            )
        )
        with pytest.raises(AdapterResponseError):
            await adapter.get_slow_service_traces(
                ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW)
            )
    finally:
        await client.close()
    assert empty.status == CollectionStatus.EMPTY


@pytest.mark.asyncio
async def test_tempo_search_restores_leading_zero_and_allows_omitted_duration() -> None:
    """Tempo legacy search may trim trace-ID zeroes and omit zero durationMs."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/search"
        return httpx.Response(
            200,
            json={
                "traces": [
                    {
                        "traceID": TRACE_ID.lstrip("0"),
                        "rootServiceName": "payment-service",
                        "rootTraceName": "connect",
                        "startTimeUnixNano": str(int(NOW.timestamp() * 1_000_000_000)),
                    }
                ]
            },
        )

    client = TempoClient(_settings(), transport=_streaming_transport(handler))
    try:
        evidence = await TempoAdapter(client).get_slow_service_traces(
            ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW)
        )
    finally:
        await client.close()

    assert evidence.status == CollectionStatus.COLLECTED
    assert evidence.payload["traces"][0]["trace_id"] == TRACE_ID  # type: ignore[index]
    assert evidence.payload["traces"][0]["duration_ms"] == 0.0  # type: ignore[index]


class _DeploymentReader:
    def __init__(self) -> None:
        self.records = (
            DeploymentRecord(
                id="DEP-A1B2C3D4E5F607081122",
                service=EvidenceService.PAYMENT,
                environment=DeploymentEnvironment.TEST,
                version="0.2.0",
                deployed_at=NOW - timedelta(minutes=5),
                commit_sha="a" * 40,
                changed_files=["apps/demo/payment_service/main.py"],
                metadata={"scenario": "fixture"},
                registered_at=NOW,
            ),
            DeploymentRecord(
                id="DEP-B1B2C3D4E5F607081122",
                service=EvidenceService.PAYMENT,
                environment=DeploymentEnvironment.TEST,
                version="0.1.0",
                deployed_at=NOW - timedelta(hours=1),
                commit_sha="b" * 40,
                changed_files=["README.md"],
                registered_at=NOW,
            ),
        )

    async def recent_deployments(self, **_kwargs: object) -> tuple[DeploymentRecord, ...]:
        return self.records

    async def current_previous_deployments(self, **_kwargs: object) -> tuple[DeploymentRecord, ...]:
        return self.records

    async def get_deployment(self, **kwargs: object) -> DeploymentRecord | None:
        return self.records[0] if kwargs["deployment_id"] == self.records[0].id else None


@pytest.mark.asyncio
async def test_deployment_adapter_exposes_only_named_history_version_and_commit_reads() -> None:
    adapter = DeploymentAdapter(DeploymentClient(_DeploymentReader()))  # type: ignore[arg-type]
    window_request = DeploymentWindowQuery(
        service=EvidenceService.PAYMENT,
        environment=DeploymentEnvironment.TEST,
        window=WINDOW,
    )
    recent = await adapter.get_recent_deployments(window_request)
    versions = await adapter.get_current_previous_version(
        DeploymentAtQuery(
            service=EvidenceService.PAYMENT,
            environment=DeploymentEnvironment.TEST,
            window=WINDOW,
            at=NOW,
        )
    )
    commit = await adapter.get_commit_metadata(
        DeploymentCommitQuery(
            deployment_id="DEP-A1B2C3D4E5F607081122",
            service=EvidenceService.PAYMENT,
            window=WINDOW,
        )
    )

    assert recent.status == versions.status == commit.status == CollectionStatus.COLLECTED
    assert versions.payload["current"]["version"] == "0.2.0"  # type: ignore[index]
    assert commit.payload["changed_files"] == ["apps/demo/payment_service/main.py"]


def test_typed_inputs_reject_query_injection_and_bounds_before_io() -> None:
    """Services, trace IDs, result limits, paths, and around timestamps are closed inputs."""

    with pytest.raises(ValidationError):
        ServiceQuery.model_validate(
            {
                "service": 'payment-service"} or vector(1)',
                "window": WINDOW.model_dump(),
            }
        )
    with pytest.raises(ValidationError):
        TraceByIdQuery(
            service=EvidenceService.PAYMENT,
            window=WINDOW,
            trace_id="../../api/search",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW, limit=101)
    with pytest.raises(ValidationError):
        LogsAroundQuery(
            service=EvidenceService.PAYMENT,
            window=WINDOW,
            timestamp=NOW + timedelta(hours=1),
            radius_seconds=301,
        )


@pytest.mark.asyncio
async def test_transport_rejects_oversized_response() -> None:
    """Response byte limits stop a streamed body before later chunks are buffered."""

    stream = _ChunkedStream([b'{"data":"', b"x" * 1_024, b"x" * 1_024, b'"}'])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client = PrometheusClient(
        _settings(evidence_max_response_bytes=1024),
        transport=_streaming_transport(handler),
    )
    try:
        with pytest.raises(AdapterResponseError, match="size limit"):
            await PrometheusAdapter(client).get_service_latency(
                ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW)
            )
    finally:
        await client.close()
    assert stream.chunks_yielded == 2


@pytest.mark.asyncio
async def test_transport_rejects_compression_without_reading_the_body() -> None:
    """A backend cannot bypass the memory bound through transparent decompression."""

    stream = _ChunkedStream([b"compressed bytes must not be read"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

    client = PrometheusClient(_settings(), transport=_streaming_transport(handler))
    try:
        with pytest.raises(AdapterResponseError, match="compressed"):
            await PrometheusAdapter(client).get_service_latency(
                ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW)
            )
    finally:
        await client.close()
    assert stream.chunks_yielded == 0


@pytest.mark.asyncio
async def test_http_retries_are_bounded() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("unavailable", request=request)

    client = LokiClient(
        _settings(evidence_http_max_attempts=2),
        transport=_streaming_transport(handler),
    )
    try:
        with pytest.raises(AdapterUnavailableError, match="unavailable"):
            await LokiAdapter(client).get_errors(
                ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW)
            )
    finally:
        await client.close()
    assert calls == 2


@pytest.mark.asyncio
async def test_effective_query_settings_change_provenance_and_stable_identity() -> None:
    """Configured step/threshold values are part of canonical query parameters."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/query_range":
            return httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "matrix", "result": []}},
            )
        return httpx.Response(200, json={"traces": []})

    transport = _streaming_transport(handler)
    request = ServiceQuery(service=EvidenceService.PAYMENT, window=WINDOW)
    prom_five = PrometheusClient(
        _settings(evidence_metric_step_seconds=5),
        transport=transport,
    )
    prom_ten = PrometheusClient(
        _settings(evidence_metric_step_seconds=10),
        transport=transport,
    )
    tempo_500 = TempoClient(
        _settings(evidence_slow_trace_threshold_ms=500),
        transport=transport,
    )
    tempo_900 = TempoClient(
        _settings(evidence_slow_trace_threshold_ms=900),
        transport=transport,
    )
    try:
        metric_five = await PrometheusAdapter(prom_five).get_service_latency(request)
        metric_ten = await PrometheusAdapter(prom_ten).get_service_latency(request)
        slow_500 = await TempoAdapter(tempo_500).get_slow_service_traces(request)
        slow_900 = await TempoAdapter(tempo_900).get_slow_service_traces(request)
    finally:
        await prom_five.close()
        await prom_ten.close()
        await tempo_500.close()
        await tempo_900.close()

    assert metric_five.query_parameters["step_seconds"] == 5
    assert metric_ten.query_parameters["step_seconds"] == 10
    assert slow_500.query_parameters["min_duration_ms"] == 500
    assert slow_900.query_parameters["min_duration_ms"] == 900
    first_id = stable_evidence_id(
        incident_id="INC-A1B2C3D4E5F60708",
        source=EvidenceSource.PROMETHEUS,
        query_template=QueryTemplate.METRIC_SERVICE_LATENCY.value,
        query_parameters=metric_five.query_parameters,
    )
    changed_id = stable_evidence_id(
        incident_id="INC-A1B2C3D4E5F60708",
        source=EvidenceSource.PROMETHEUS,
        query_template=QueryTemplate.METRIC_SERVICE_LATENCY.value,
        query_parameters=metric_ten.query_parameters,
    )
    assert first_id != changed_id
    slow_500_id = stable_evidence_id(
        incident_id="INC-A1B2C3D4E5F60708",
        source=EvidenceSource.TEMPO,
        query_template=QueryTemplate.TRACE_SLOW_SERVICE.value,
        query_parameters=slow_500.query_parameters,
    )
    slow_900_id = stable_evidence_id(
        incident_id="INC-A1B2C3D4E5F60708",
        source=EvidenceSource.TEMPO,
        query_template=QueryTemplate.TRACE_SLOW_SERVICE.value,
        query_parameters=slow_900.query_parameters,
    )
    assert slow_500_id != slow_900_id
