"""Typed Loki client and allowlisted, locally sanitized log domain adapter."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.config import Settings
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceSource,
    EvidenceType,
    EvidenceWindow,
    LogsAroundQuery,
    QueryTemplate,
    ServiceQuery,
)
from packages.telemetry import TelemetryRuntime, redact_text, redact_value
from packages.tools.http import AdapterQueryError, AdapterResponseError, BoundedJsonClient

_LOG_TEMPLATES = frozenset(
    {
        QueryTemplate.LOG_SERVICE_ERRORS,
        QueryTemplate.LOG_GROUPED_PATTERNS,
        QueryTemplate.LOG_AROUND_TIMESTAMP,
    }
)
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
_HEX_PATTERN = re.compile(r"\b[0-9a-fA-F]{16,64}\b")
_NUMBER_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")


class _LokiStream(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stream: Annotated[dict[str, str], Field(max_length=64)]
    values: Annotated[list[list[str]], Field(max_length=10_000)]


class _LokiData(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    result_type: str = Field(alias="resultType")
    result: list[_LokiStream]


class _LokiSuccess(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    status: str
    data: _LokiData


class _LogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    severity: str
    event: str
    trace_id: str | None = None
    span_id: str | None = None
    request_id: str | None = None
    attributes: dict[str, object] = Field(default_factory=dict)


class LokiClient(BoundedJsonClient):
    """Low-level Loki range client with fixed LogQL renderers only."""

    def __init__(
        self,
        settings: Settings,
        *,
        telemetry: TelemetryRuntime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            source=EvidenceSource.LOKI.value,
            base_url=str(settings.loki_url),
            timeout_seconds=settings.evidence_http_timeout_seconds,
            max_attempts=settings.evidence_http_max_attempts,
            retry_backoff_seconds=settings.evidence_http_retry_backoff_seconds,
            max_response_bytes=settings.evidence_max_response_bytes,
            telemetry=telemetry,
            transport=transport,
        )

    async def query_range(
        self,
        template: QueryTemplate,
        request: ServiceQuery | LogsAroundQuery,
    ) -> tuple[_LokiStream, ...]:
        """Render and execute one fixed LogQL stream query."""

        if template not in _LOG_TEMPLATES:
            raise ValueError("Loki client received a non-log template")
        if template == QueryTemplate.LOG_AROUND_TIMESTAMP:
            if not isinstance(request, LogsAroundQuery):
                raise ValueError("around-timestamp logs require a typed timestamp request")
            start = max(
                request.window.start,
                request.timestamp - timedelta(seconds=request.radius_seconds),
            )
            end = min(
                request.window.end,
                request.timestamp + timedelta(seconds=request.radius_seconds),
            )
        else:
            start = request.window.start
            end = request.window.end
        payload = await self._get_json(
            path="/loki/api/v1/query_range",
            params={
                "query": _render_logql(template, request.service.value),
                "start": str(int(start.timestamp() * 1_000_000_000)),
                "end": str(int(end.timestamp() * 1_000_000_000)),
                "direction": "forward",
                "limit": request.limit,
            },
        )
        if payload is None:
            raise AdapterResponseError("Loki returned an empty HTTP response")
        if payload.get("status") != "success":
            raise AdapterQueryError("Loki rejected a fixed range query")
        try:
            parsed = _LokiSuccess.model_validate(payload)
        except ValidationError as error:
            raise AdapterResponseError("Loki returned a malformed range response") from error
        if parsed.data.result_type != "streams":
            raise AdapterResponseError("Loki returned an unexpected result type")
        return tuple(parsed.data.result)


class LokiAdapter:
    """Allowlisted log retrieval and safe local grouping domain methods."""

    def __init__(self, client: LokiClient) -> None:
        self._client = client

    async def get_errors(self, request: ServiceQuery) -> EvidenceDraft:
        entries = _normalize_streams(
            await self._client.query_range(QueryTemplate.LOG_SERVICE_ERRORS, request),
            limit=request.limit,
        )
        return _entries_draft(
            template=QueryTemplate.LOG_SERVICE_ERRORS,
            request=request,
            entries=entries,
            description="error logs",
        )

    async def get_grouped_patterns(self, request: ServiceQuery) -> EvidenceDraft:
        entries = _normalize_streams(
            await self._client.query_range(QueryTemplate.LOG_GROUPED_PATTERNS, request),
            limit=request.limit,
        )
        parameters = _query_parameters(request)
        if not entries:
            return EvidenceDraft(
                source=EvidenceSource.LOKI,
                type=EvidenceType.LOG,
                status=CollectionStatus.EMPTY,
                observed_at=request.window.end,
                window=request.window,
                summary=f"No log patterns were available for {request.service.value}",
                payload={},
                query_template=QueryTemplate.LOG_GROUPED_PATTERNS,
                query_parameters=parameters,
                provenance={"adapter": "loki", "api": "v1/query_range", "grouping": "local"},
            )
        grouped: dict[tuple[str, str], dict[str, object]] = {}
        for entry in entries:
            key = (entry.severity, _safe_pattern(entry.event))
            group = grouped.setdefault(
                key,
                {
                    "severity": entry.severity,
                    "pattern": key[1],
                    "count": 0,
                    "first_timestamp": entry.timestamp.isoformat(),
                    "last_timestamp": entry.timestamp.isoformat(),
                },
            )
            current_count = group.get("count")
            group["count"] = (current_count if isinstance(current_count, int) else 0) + 1
            group["last_timestamp"] = entry.timestamp.isoformat()
        patterns = sorted(
            grouped.values(),
            key=_pattern_sort_key,
        )
        return EvidenceDraft(
            source=EvidenceSource.LOKI,
            type=EvidenceType.LOG,
            status=CollectionStatus.COLLECTED,
            observed_at=max(entry.timestamp for entry in entries),
            window=request.window,
            summary=(
                f"Grouped {len(entries)} logs for {request.service.value} "
                f"into {len(patterns)} safe patterns"
            ),
            payload={"patterns": patterns},
            query_template=QueryTemplate.LOG_GROUPED_PATTERNS,
            query_parameters=parameters,
            provenance={"adapter": "loki", "api": "v1/query_range", "grouping": "local"},
        )

    async def get_logs_around(self, request: LogsAroundQuery) -> EvidenceDraft:
        entries = _normalize_streams(
            await self._client.query_range(QueryTemplate.LOG_AROUND_TIMESTAMP, request),
            limit=request.limit,
        )
        actual_window = EvidenceWindow(
            start=max(
                request.window.start,
                request.timestamp - timedelta(seconds=request.radius_seconds),
            ),
            end=min(
                request.window.end,
                request.timestamp + timedelta(seconds=request.radius_seconds),
            ),
        )
        return _entries_draft(
            template=QueryTemplate.LOG_AROUND_TIMESTAMP,
            request=request,
            entries=entries,
            description="logs around the incident timestamp",
            window=actual_window,
            extra_parameters={
                "timestamp": request.timestamp.isoformat(),
                "radius_seconds": request.radius_seconds,
            },
        )


def _normalize_streams(
    streams: tuple[_LokiStream, ...],
    *,
    limit: int,
) -> tuple[_LogEntry, ...]:
    entries: list[_LogEntry] = []
    for stream in streams:
        for value in stream.values:
            if len(value) != 2:
                raise AdapterResponseError("Loki returned a malformed stream value")
            raw_timestamp, line = value
            try:
                timestamp = datetime.fromtimestamp(int(raw_timestamp) / 1_000_000_000, tz=UTC)
            except (OverflowError, ValueError) as error:
                raise AdapterResponseError("Loki returned an invalid timestamp") from error
            entries.append(_normalize_line(timestamp, line))
    entries.sort(key=lambda item: (item.timestamp, item.trace_id or "", item.event))
    if len(entries) > limit:
        raise AdapterResponseError("Loki returned more log entries than requested")
    return tuple(entries)


def _normalize_line(timestamp: datetime, line: str) -> _LogEntry:
    try:
        parsed = json.loads(line)
    except (TypeError, ValueError):
        parsed = {"event": _safe_pattern(redact_text(line)), "severity": "UNKNOWN"}
    if not isinstance(parsed, dict):
        parsed = {"event": "unstructured_log", "severity": "UNKNOWN"}
    safe = redact_value(parsed)
    if not isinstance(safe, dict):
        safe = {}
    event = safe.get("event")
    severity = safe.get("severity")
    attributes = safe.get("attributes")
    return _LogEntry(
        timestamp=timestamp,
        event=redact_text(str(event))[:256] if event else "unstructured_log",
        severity=redact_text(str(severity))[:32] if severity else "UNKNOWN",
        trace_id=_optional_text(safe.get("trace_id"), 64),
        span_id=_optional_text(safe.get("span_id"), 32),
        request_id=_optional_text(safe.get("request_id"), 64),
        attributes=attributes if isinstance(attributes, dict) else {},
    )


def _entries_draft(
    *,
    template: QueryTemplate,
    request: ServiceQuery,
    entries: tuple[_LogEntry, ...],
    description: str,
    window: EvidenceWindow | None = None,
    extra_parameters: dict[str, object] | None = None,
) -> EvidenceDraft:
    evidence_window = window or request.window
    parameters = _query_parameters(request)
    if extra_parameters:
        parameters.update(extra_parameters)
    if not entries:
        return EvidenceDraft(
            source=EvidenceSource.LOKI,
            type=EvidenceType.LOG,
            status=CollectionStatus.EMPTY,
            observed_at=evidence_window.end,
            window=evidence_window,
            summary=f"No {description} were available for {request.service.value}",
            payload={},
            query_template=template,
            query_parameters=parameters,
            provenance={"adapter": "loki", "api": "v1/query_range"},
        )
    payload_entries = [entry.model_dump(mode="json") for entry in entries]
    return EvidenceDraft(
        source=EvidenceSource.LOKI,
        type=EvidenceType.LOG,
        status=CollectionStatus.COLLECTED,
        observed_at=max(entry.timestamp for entry in entries),
        window=evidence_window,
        summary=f"Collected {len(entries)} {description} for {request.service.value}",
        payload={"entries": payload_entries},
        query_template=template,
        query_parameters=parameters,
        provenance={"adapter": "loki", "api": "v1/query_range"},
    )


def _render_logql(template: QueryTemplate, service: str) -> str:
    selector = f'{{service_name="{service}"}}'
    if template == QueryTemplate.LOG_SERVICE_ERRORS:
        return f'{selector} | json | severity=~"ERROR|CRITICAL"'
    return selector


def _query_parameters(request: ServiceQuery) -> dict[str, object]:
    return {
        "service": request.service.value,
        "window_start": request.window.start.isoformat(),
        "window_end": request.window.end.isoformat(),
        "entry_limit": request.limit,
    }


def _safe_pattern(value: str) -> str:
    normalized = _UUID_PATTERN.sub("<uuid>", value)
    normalized = _HEX_PATTERN.sub("<hex>", normalized)
    normalized = _NUMBER_PATTERN.sub("<number>", normalized)
    return redact_text(normalized)[:256] or "empty_log"


def _optional_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    return redact_text(str(value))[:limit] or None


def _pattern_sort_key(item: dict[str, object]) -> tuple[int, str, str]:
    count = item.get("count")
    return (
        -(count if isinstance(count, int) else 0),
        str(item.get("severity", "")),
        str(item.get("pattern", "")),
    )
