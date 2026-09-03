"""Typed Prometheus client and allowlisted service-metric domain adapter."""

import math
from datetime import UTC, datetime
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.config import Settings
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceSource,
    EvidenceType,
    QueryTemplate,
    ServiceQuery,
)
from packages.telemetry import TelemetryRuntime, redact_value
from packages.tools.http import AdapterQueryError, AdapterResponseError, BoundedJsonClient

_METRIC_TEMPLATES = frozenset(
    {
        QueryTemplate.METRIC_SERVICE_LATENCY,
        QueryTemplate.METRIC_SERVICE_ERROR_RATE,
        QueryTemplate.METRIC_SERVICE_CPU,
        QueryTemplate.METRIC_SERVICE_MEMORY,
        QueryTemplate.METRIC_DB_POOL_USAGE,
    }
)


class _PrometheusSeries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: Annotated[dict[str, str], Field(max_length=64)]
    values: Annotated[list[tuple[float, str]], Field(max_length=10_000)]


class _PrometheusData(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    result_type: str = Field(alias="resultType")
    result: list[_PrometheusSeries]


class _PrometheusSuccess(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    status: str
    data: _PrometheusData


class PrometheusClient(BoundedJsonClient):
    """Low-level Prometheus range client that accepts only repository template IDs."""

    def __init__(
        self,
        settings: Settings,
        *,
        telemetry: TelemetryRuntime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            source=EvidenceSource.PROMETHEUS.value,
            base_url=str(settings.prometheus_url),
            timeout_seconds=settings.evidence_http_timeout_seconds,
            max_attempts=settings.evidence_http_max_attempts,
            retry_backoff_seconds=settings.evidence_http_retry_backoff_seconds,
            max_response_bytes=settings.evidence_max_response_bytes,
            telemetry=telemetry,
            transport=transport,
        )
        self._step_seconds = settings.evidence_metric_step_seconds

    @property
    def step_seconds(self) -> int:
        """Return the repository-owned range step used by every metric template."""

        return self._step_seconds

    async def query_range(
        self,
        template: QueryTemplate,
        request: ServiceQuery,
    ) -> tuple[_PrometheusSeries, ...]:
        """Render and execute one fixed PromQL template."""

        if template not in _METRIC_TEMPLATES:
            raise ValueError("Prometheus client received a non-metric template")
        query = _render_promql(template, request.service.value)
        payload = await self._get_json(
            path="/api/v1/query_range",
            params={
                "query": query,
                "start": request.window.start.timestamp(),
                "end": request.window.end.timestamp(),
                "step": self._step_seconds,
            },
        )
        if payload is None:
            raise AdapterResponseError("Prometheus returned an empty HTTP response")
        if payload.get("status") != "success":
            raise AdapterQueryError("Prometheus rejected a fixed range query")
        try:
            parsed = _PrometheusSuccess.model_validate(payload)
        except ValidationError as error:
            raise AdapterResponseError("Prometheus returned a malformed range response") from error
        if parsed.data.result_type != "matrix":
            raise AdapterResponseError("Prometheus returned an unexpected result type")
        if len(parsed.data.result) > request.limit:
            raise AdapterResponseError("Prometheus returned more series than requested")
        return tuple(parsed.data.result)


class PrometheusAdapter:
    """Allowlisted metrics domain methods; no arbitrary PromQL is exposed."""

    def __init__(self, client: PrometheusClient) -> None:
        self._client = client

    async def get_service_latency(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._collect(
            QueryTemplate.METRIC_SERVICE_LATENCY,
            request,
            unit="seconds",
            statistic="p95",
            display_name="service p95 latency",
        )

    async def get_service_error_rate(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._collect(
            QueryTemplate.METRIC_SERVICE_ERROR_RATE,
            request,
            unit="ratio",
            statistic="rate",
            display_name="service error rate",
        )

    async def get_service_cpu(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._collect(
            QueryTemplate.METRIC_SERVICE_CPU,
            request,
            unit="cores",
            statistic="rate",
            display_name="service CPU usage",
        )

    async def get_service_memory(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._collect(
            QueryTemplate.METRIC_SERVICE_MEMORY,
            request,
            unit="bytes",
            statistic="maximum",
            display_name="service resident memory",
        )

    async def get_db_pool_usage(self, request: ServiceQuery) -> EvidenceDraft:
        return await self._collect(
            QueryTemplate.METRIC_DB_POOL_USAGE,
            request,
            unit="connections",
            statistic="maximum",
            display_name="database pool usage",
        )

    async def _collect(
        self,
        template: QueryTemplate,
        request: ServiceQuery,
        *,
        unit: str,
        statistic: str,
        display_name: str,
    ) -> EvidenceDraft:
        series = await self._client.query_range(template, request)
        normalized: list[dict[str, object]] = []
        finite_values: list[tuple[datetime, float]] = []
        for item in series:
            samples: list[dict[str, object]] = []
            for timestamp, raw_value in item.values:
                try:
                    value = float(raw_value)
                except ValueError as error:
                    raise AdapterResponseError(
                        "Prometheus returned a non-numeric sample"
                    ) from error
                if not math.isfinite(value) or not math.isfinite(timestamp):
                    continue
                observed_at = datetime.fromtimestamp(timestamp, tz=UTC)
                finite_values.append((observed_at, value))
                samples.append({"timestamp": observed_at.isoformat(), "value": value})
            if samples:
                safe_labels = redact_value(item.metric)
                normalized.append(
                    {
                        "labels": safe_labels if isinstance(safe_labels, dict) else {},
                        "samples": samples,
                    }
                )
        parameters = _query_parameters(request, step_seconds=self._client.step_seconds)
        if not finite_values:
            return EvidenceDraft(
                source=EvidenceSource.PROMETHEUS,
                type=EvidenceType.METRIC,
                status=CollectionStatus.EMPTY,
                observed_at=request.window.end,
                window=request.window,
                summary=f"No {display_name} samples were available for {request.service.value}",
                payload={},
                query_template=template,
                query_parameters=parameters,
                provenance={"adapter": "prometheus", "api": "v1/query_range"},
            )
        observed_at = max(timestamp for timestamp, _value in finite_values)
        maximum = max(value for _timestamp, value in finite_values)
        return EvidenceDraft(
            source=EvidenceSource.PROMETHEUS,
            type=EvidenceType.METRIC,
            status=CollectionStatus.COLLECTED,
            observed_at=observed_at,
            window=request.window,
            summary=(
                f"{display_name.capitalize()} for {request.service.value}: "
                f"maximum {maximum:.6g} {unit}"
            ),
            payload={"series": normalized, "unit": unit, "statistic": statistic},
            query_template=template,
            query_parameters=parameters,
            provenance={"adapter": "prometheus", "api": "v1/query_range"},
        )


def _render_promql(template: QueryTemplate, service: str) -> str:
    selectors = {
        QueryTemplate.METRIC_SERVICE_LATENCY: (
            "histogram_quantile(0.95, sum by (le) "
            f'(rate(demo_http_request_duration_seconds_bucket{{service="{service}"}}[20s])))'
        ),
        QueryTemplate.METRIC_SERVICE_ERROR_RATE: (
            f'sum(rate(demo_http_request_errors_total{{service="{service}"}}[20s])) / '
            "clamp_min("
            f'sum(rate(demo_http_requests_total{{service="{service}"}}[20s])), 1e-9)'
        ),
        QueryTemplate.METRIC_SERVICE_CPU: (
            f'sum(rate(process_cpu_seconds_total{{job="{service}"}}[1m]))'
        ),
        QueryTemplate.METRIC_SERVICE_MEMORY: (
            f'max(process_resident_memory_bytes{{job="{service}"}})'
        ),
        QueryTemplate.METRIC_DB_POOL_USAGE: (f'max(demo_db_pool_in_use{{service="{service}"}})'),
    }
    return selectors[template]


def _query_parameters(request: ServiceQuery, *, step_seconds: int) -> dict[str, object]:
    return {
        "service": request.service.value,
        "window_start": request.window.start.isoformat(),
        "window_end": request.window.end.isoformat(),
        "series_limit": request.limit,
        "step_seconds": step_seconds,
    }
