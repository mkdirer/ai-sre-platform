"""Typed Tempo client and allowlisted trace/dependency domain adapter."""

import asyncio
import base64
import binascii
import re
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from packages.config import Settings
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceSource,
    EvidenceType,
    QueryTemplate,
    ServiceQuery,
    TraceByIdQuery,
)
from packages.telemetry import TelemetryRuntime, redact_text, redact_value
from packages.tools.http import AdapterQueryError, AdapterResponseError, BoundedJsonClient

_TRACE_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_TEMPO_SEARCH_TRACE_ID_PATTERN = re.compile(r"^[a-fA-F0-9]{1,32}$")
_TRACE_SEARCH_TEMPLATES = frozenset(
    {
        QueryTemplate.TRACE_SLOW_SERVICE,
        QueryTemplate.TRACE_SERVICE_DEPENDENCIES,
    }
)
_DEPENDENCY_MIN_DURATION_MS = 50


class _TempoSearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    trace_id: str = Field(alias="traceID")
    root_service_name: str = Field(default="", alias="rootServiceName")
    root_trace_name: str = Field(default="", alias="rootTraceName")
    start_time_unix_nano: str = Field(alias="startTimeUnixNano")
    duration_ms: float = Field(default=0.0, alias="durationMs")

    @field_validator("trace_id")
    @classmethod
    def normalize_trace_id(cls, value: str) -> str:
        """Restore leading zeroes omitted by Tempo's legacy search JSON encoder."""

        if _TEMPO_SEARCH_TRACE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("Tempo search trace ID is not hexadecimal")
        return value.lower().zfill(32)


class _TempoSearchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    traces: list[_TempoSearchResult]


class _OtlpValue(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    string_value: str | None = Field(default=None, alias="stringValue")
    bool_value: bool | None = Field(default=None, alias="boolValue")
    int_value: str | int | None = Field(default=None, alias="intValue")
    double_value: float | None = Field(default=None, alias="doubleValue")


class _OtlpAttribute(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    key: str
    value: _OtlpValue


class _TempoResource(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    attributes: list[_OtlpAttribute] = Field(default_factory=list)


class _TempoSpan(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    trace_id: str = Field(alias="traceId")
    span_id: str = Field(alias="spanId")
    parent_span_id: str = Field(default="", alias="parentSpanId")
    name: str
    start_time_unix_nano: str = Field(alias="startTimeUnixNano")
    end_time_unix_nano: str = Field(alias="endTimeUnixNano")
    attributes: list[_OtlpAttribute] = Field(default_factory=list)

    @field_validator("trace_id")
    @classmethod
    def normalize_trace_id(cls, value: str) -> str:
        return _normalize_otlp_id(value, byte_length=16, field_name="trace ID")

    @field_validator("span_id")
    @classmethod
    def normalize_span_id(cls, value: str) -> str:
        return _normalize_otlp_id(value, byte_length=8, field_name="span ID")

    @field_validator("parent_span_id")
    @classmethod
    def normalize_parent_span_id(cls, value: str) -> str:
        if not value:
            return value
        return _normalize_otlp_id(value, byte_length=8, field_name="parent span ID")


class _TempoScopeSpans(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    spans: list[_TempoSpan]


class _TempoBatch(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    resource: _TempoResource
    scope_spans: list[_TempoScopeSpans] = Field(alias="scopeSpans")


class _TempoTraceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    batches: list[_TempoBatch]


class TempoClient(BoundedJsonClient):
    """Low-level Tempo search/trace client with fixed search templates."""

    def __init__(
        self,
        settings: Settings,
        *,
        telemetry: TelemetryRuntime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            source=EvidenceSource.TEMPO.value,
            base_url=str(settings.tempo_url),
            timeout_seconds=settings.evidence_http_timeout_seconds,
            max_attempts=settings.evidence_http_max_attempts,
            retry_backoff_seconds=settings.evidence_http_retry_backoff_seconds,
            max_response_bytes=settings.evidence_max_response_bytes,
            telemetry=telemetry,
            transport=transport,
        )
        self._slow_trace_threshold_ms = settings.evidence_slow_trace_threshold_ms

    @property
    def slow_trace_threshold_ms(self) -> int:
        """Return the configured threshold that changes slow-trace query semantics."""

        return self._slow_trace_threshold_ms

    async def search(
        self,
        template: QueryTemplate,
        request: ServiceQuery,
    ) -> tuple[_TempoSearchResult, ...]:
        """Execute a fixed service-tag search, optionally with a fixed duration bound."""

        if template not in _TRACE_SEARCH_TEMPLATES:
            raise ValueError("Tempo client received a non-search template")
        params: dict[str, str | int | float] = {
            "tags": f"service.name={request.service.value}",
            "start": int(request.window.start.timestamp()),
            "end": int(request.window.end.timestamp()),
            "limit": request.limit,
        }
        if template == QueryTemplate.TRACE_SLOW_SERVICE:
            params["minDuration"] = f"{self._slow_trace_threshold_ms}ms"
        elif template == QueryTemplate.TRACE_SERVICE_DEPENDENCIES:
            params["minDuration"] = f"{_DEPENDENCY_MIN_DURATION_MS}ms"
        payload = await self._get_json(path="/api/search", params=params)
        if payload is None:
            raise AdapterResponseError("Tempo returned an empty HTTP response")
        if "error" in payload:
            raise AdapterQueryError("Tempo rejected a fixed trace search")
        try:
            parsed = _TempoSearchResponse.model_validate(payload)
        except ValidationError as error:
            raise AdapterResponseError("Tempo returned a malformed search response") from error
        if len(parsed.traces) > request.limit:
            raise AdapterResponseError("Tempo returned more traces than requested")
        for trace in parsed.traces:
            if _TRACE_ID_PATTERN.fullmatch(trace.trace_id) is None:
                raise AdapterResponseError("Tempo returned an invalid trace ID")
            _nano_timestamp(trace.start_time_unix_nano)
            if trace.duration_ms < 0:
                raise AdapterResponseError("Tempo returned a negative trace duration")
        return tuple(parsed.traces)

    async def trace_by_id(self, request: TraceByIdQuery) -> _TempoTraceResponse | None:
        """Fetch one validated trace ID from the fixed trace endpoint."""

        payload = await self._get_json(
            path=f"/api/traces/{request.trace_id}",
            params={},
            not_found_is_empty=True,
        )
        if payload is None:
            return None
        try:
            parsed = _TempoTraceResponse.model_validate(payload)
        except ValidationError as error:
            raise AdapterResponseError("Tempo returned a malformed trace response") from error
        for batch in parsed.batches:
            for scope in batch.scope_spans:
                for span in scope.spans:
                    if span.trace_id != request.trace_id:
                        raise AdapterResponseError("Tempo trace response mixed trace identities")
                    _span_times(span)
        return parsed


class TempoAdapter:
    """Allowlisted trace lookup, slow-trace, and dependency evidence methods."""

    def __init__(self, client: TempoClient) -> None:
        self._client = client

    async def get_trace_by_id(self, request: TraceByIdQuery) -> EvidenceDraft:
        trace = await self._client.trace_by_id(request)
        parameters = _query_parameters(request) | {"trace_id": request.trace_id}
        if trace is None:
            return EvidenceDraft(
                source=EvidenceSource.TEMPO,
                type=EvidenceType.TRACE,
                status=CollectionStatus.EMPTY,
                observed_at=request.window.end,
                window=request.window,
                summary=f"Trace {request.trace_id} was not available in Tempo",
                payload={},
                query_template=QueryTemplate.TRACE_BY_ID,
                query_parameters=parameters,
                provenance={"adapter": "tempo", "api": "api/traces"},
            )
        spans = _flatten_trace(trace)
        if not spans:
            raise AdapterResponseError("Tempo returned a trace without spans")
        services = sorted({str(span["service"]) for span in spans})
        return EvidenceDraft(
            source=EvidenceSource.TEMPO,
            type=EvidenceType.TRACE,
            status=CollectionStatus.COLLECTED,
            observed_at=max(_parse_iso(str(span["end_time"])) for span in spans),
            window=request.window,
            summary=(
                f"Trace {request.trace_id} contains {len(spans)} spans across "
                f"{len(services)} services"
            ),
            payload={"trace_id": request.trace_id, "services": services, "spans": spans},
            query_template=QueryTemplate.TRACE_BY_ID,
            query_parameters=parameters,
            provenance={"adapter": "tempo", "api": "api/traces"},
        )

    async def get_slow_service_traces(self, request: ServiceQuery) -> EvidenceDraft:
        traces = await self._client.search(QueryTemplate.TRACE_SLOW_SERVICE, request)
        parameters = _query_parameters(request) | {
            "min_duration_ms": self._client.slow_trace_threshold_ms,
        }
        if not traces:
            return EvidenceDraft(
                source=EvidenceSource.TEMPO,
                type=EvidenceType.TRACE,
                status=CollectionStatus.EMPTY,
                observed_at=request.window.end,
                window=request.window,
                summary=f"No slow traces were available for {request.service.value}",
                payload={},
                query_template=QueryTemplate.TRACE_SLOW_SERVICE,
                query_parameters=parameters,
                provenance={"adapter": "tempo", "api": "api/search"},
            )
        normalized = [_normalize_search_result(trace) for trace in traces]
        return EvidenceDraft(
            source=EvidenceSource.TEMPO,
            type=EvidenceType.TRACE,
            status=CollectionStatus.COLLECTED,
            observed_at=max(_parse_iso(str(trace["timestamp"])) for trace in normalized),
            window=request.window,
            summary=f"Found {len(normalized)} slow traces for {request.service.value}",
            payload={"traces": normalized},
            query_template=QueryTemplate.TRACE_SLOW_SERVICE,
            query_parameters=parameters,
            provenance={"adapter": "tempo", "api": "api/search"},
        )

    async def get_service_dependencies(self, request: ServiceQuery) -> EvidenceDraft:
        summaries = await self._client.search(
            QueryTemplate.TRACE_SERVICE_DEPENDENCIES,
            request,
        )
        parameters = _query_parameters(request) | {
            "min_duration_ms": _DEPENDENCY_MIN_DURATION_MS,
        }
        if not summaries:
            return EvidenceDraft(
                source=EvidenceSource.TEMPO,
                type=EvidenceType.TRACE,
                status=CollectionStatus.EMPTY,
                observed_at=request.window.end,
                window=request.window,
                summary=f"No dependency traces were available for {request.service.value}",
                payload={},
                query_template=QueryTemplate.TRACE_SERVICE_DEPENDENCIES,
                query_parameters=parameters,
                provenance={"adapter": "tempo", "api": "api/search+api/traces"},
            )
        trace_requests = [
            TraceByIdQuery(
                service=request.service,
                window=request.window,
                limit=request.limit,
                trace_id=summary.trace_id,
            )
            for summary in summaries
        ]
        traces = await asyncio.gather(
            *(self._client.trace_by_id(trace_request) for trace_request in trace_requests)
        )
        edges: dict[tuple[str, str], dict[str, object]] = {}
        for summary, trace in zip(summaries, traces, strict=True):
            if trace is None:
                continue
            span_services: dict[str, str] = {}
            spans: list[_TempoSpan] = []
            for batch in trace.batches:
                service = _service_name(batch.resource)
                for scope in batch.scope_spans:
                    for span in scope.spans:
                        span_services[span.span_id] = service
                        spans.append(span)
            for span in spans:
                parent_service = span_services.get(span.parent_span_id)
                child_service = span_services.get(span.span_id, "unknown")
                if parent_service is None or parent_service == child_service:
                    continue
                key = (parent_service, child_service)
                edge = edges.setdefault(
                    key,
                    {
                        "parent_service": parent_service,
                        "child_service": child_service,
                        "trace_count": 0,
                        "trace_ids": [],
                    },
                )
                trace_ids = edge["trace_ids"]
                if isinstance(trace_ids, list) and summary.trace_id not in trace_ids:
                    trace_ids.append(summary.trace_id)
                    current_count = edge.get("trace_count")
                    edge["trace_count"] = (
                        current_count if isinstance(current_count, int) else 0
                    ) + 1
        if not edges:
            return EvidenceDraft(
                source=EvidenceSource.TEMPO,
                type=EvidenceType.TRACE,
                status=CollectionStatus.EMPTY,
                observed_at=max(_search_timestamp(summary) for summary in summaries),
                window=request.window,
                summary=f"No cross-service dependency edges were found for {request.service.value}",
                payload={"inspected_trace_count": len(summaries)},
                query_template=QueryTemplate.TRACE_SERVICE_DEPENDENCIES,
                query_parameters=parameters,
                provenance={"adapter": "tempo", "api": "api/search+api/traces"},
            )
        normalized_edges = sorted(
            edges.values(),
            key=lambda edge: (str(edge["parent_service"]), str(edge["child_service"])),
        )
        return EvidenceDraft(
            source=EvidenceSource.TEMPO,
            type=EvidenceType.TRACE,
            status=CollectionStatus.COLLECTED,
            observed_at=max(_search_timestamp(summary) for summary in summaries),
            window=request.window,
            summary=(
                f"Found {len(normalized_edges)} cross-service dependency edges "
                f"for {request.service.value}"
            ),
            payload={
                "inspected_trace_count": len(summaries),
                "dependencies": normalized_edges,
            },
            query_template=QueryTemplate.TRACE_SERVICE_DEPENDENCIES,
            query_parameters=parameters,
            provenance={"adapter": "tempo", "api": "api/search+api/traces"},
        )


def _flatten_trace(trace: _TempoTraceResponse) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for batch in trace.batches:
        service = _service_name(batch.resource)
        for scope in batch.scope_spans:
            for span in scope.spans:
                start, end = _span_times(span)
                attributes = {
                    attribute.key: _attribute_value(attribute.value)
                    for attribute in span.attributes[:32]
                }
                safe_attributes = redact_value(attributes)
                normalized.append(
                    {
                        "service": service,
                        "span_id": span.span_id,
                        "parent_span_id": span.parent_span_id or None,
                        "name": redact_text(span.name)[:256],
                        "start_time": start.isoformat(),
                        "end_time": end.isoformat(),
                        "duration_ms": (end - start).total_seconds() * 1_000,
                        "attributes": (
                            safe_attributes if isinstance(safe_attributes, dict) else {}
                        ),
                    }
                )
    normalized.sort(key=lambda span: (str(span["start_time"]), str(span["span_id"])))
    return normalized


def _normalize_search_result(trace: _TempoSearchResult) -> dict[str, object]:
    return {
        "trace_id": trace.trace_id,
        "root_service": redact_text(trace.root_service_name)[:128],
        "root_name": redact_text(trace.root_trace_name)[:256],
        "timestamp": _search_timestamp(trace).isoformat(),
        "duration_ms": trace.duration_ms,
    }


def _service_name(resource: _TempoResource) -> str:
    for attribute in resource.attributes:
        if attribute.key == "service.name" and attribute.value.string_value:
            return redact_text(attribute.value.string_value)[:128]
    return "unknown"


def _attribute_value(value: _OtlpValue) -> object:
    if value.string_value is not None:
        return value.string_value
    if value.bool_value is not None:
        return value.bool_value
    if value.int_value is not None:
        try:
            return int(value.int_value)
        except ValueError:
            return str(value.int_value)
    if value.double_value is not None:
        return value.double_value
    return None


def _span_times(span: _TempoSpan) -> tuple[datetime, datetime]:
    start = _nano_timestamp(span.start_time_unix_nano)
    end = _nano_timestamp(span.end_time_unix_nano)
    if end < start:
        raise AdapterResponseError("Tempo returned a span with negative duration")
    return start, end


def _search_timestamp(trace: _TempoSearchResult) -> datetime:
    return _nano_timestamp(trace.start_time_unix_nano)


def _nano_timestamp(raw: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(raw) / 1_000_000_000, tz=UTC)
    except (OverflowError, ValueError) as error:
        raise AdapterResponseError("Tempo returned an invalid nanosecond timestamp") from error


def _parse_iso(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise AdapterResponseError("normalized Tempo timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise AdapterResponseError("normalized Tempo timestamp is not UTC-aware")
    return parsed.astimezone(UTC)


def _normalize_otlp_id(value: str, *, byte_length: int, field_name: str) -> str:
    """Normalize protobuf-JSON base64 IDs while retaining hex fixture compatibility."""

    hex_length = byte_length * 2
    if re.fullmatch(rf"[a-fA-F0-9]{{{hex_length}}}", value) is not None:
        return value.lower()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"Tempo returned an invalid {field_name}") from error
    if len(decoded) != byte_length:
        raise ValueError(f"Tempo returned an invalid {field_name}")
    return decoded.hex()


def _query_parameters(request: ServiceQuery) -> dict[str, object]:
    return {
        "service": request.service.value,
        "window_start": request.window.start.isoformat(),
        "window_end": request.window.end.isoformat(),
        "trace_limit": request.limit,
    }
