"""Bounded-cardinality Prometheus RED metrics."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import generate_latest

METRIC_LABEL_POLICY: dict[str, tuple[str, ...]] = {
    "demo_http_requests_total": ("service", "method", "route", "status_class"),
    "demo_http_request_errors_total": ("service", "method", "route", "error_type"),
    "demo_http_request_duration_seconds": ("service", "method", "route"),
    "demo_http_requests_in_progress": ("service", "method"),
    "demo_fault_enabled": ("service", "fault"),
    "investigator_adapter_calls_total": ("source", "template", "outcome"),
    "investigator_adapter_call_duration_seconds": ("source", "template"),
    "investigator_evidence_collection_duration_seconds": ("outcome",),
    "investigator_evidence_collection_errors_total": ("source", "outcome"),
}
_ALLOWED_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_ROUTE_PATTERN = re.compile(r"^/[A-Za-z0-9_./{}-]{0,127}$")
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def normalized_method(method: str) -> str:
    """Collapse arbitrary methods into a fixed allowlist."""

    normalized = method.upper()
    return normalized if normalized in _ALLOWED_METHODS else "OTHER"


def normalized_route(scope: Mapping[str, object]) -> str:
    """Use only framework route templates, never caller-controlled URL paths."""

    route = scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and _ROUTE_PATTERN.fullmatch(route_path) is not None:
        return route_path
    return "_unmatched"


def status_class(status_code: int) -> str:
    """Collapse status codes to a bounded class label."""

    if 100 <= status_code <= 599:
        return f"{status_code // 100}xx"
    return "unknown"


@dataclass
class HttpMetrics:
    """Per-process custom registry for application request metrics."""

    service_name: str
    registry: CollectorRegistry = field(default_factory=CollectorRegistry)

    def __post_init__(self) -> None:
        self._requests = Counter(
            "demo_http_requests_total",
            "Completed HTTP requests.",
            METRIC_LABEL_POLICY["demo_http_requests_total"],
            registry=self.registry,
        )
        self._errors = Counter(
            "demo_http_request_errors_total",
            "Completed HTTP requests with a 4xx or 5xx response.",
            METRIC_LABEL_POLICY["demo_http_request_errors_total"],
            registry=self.registry,
        )
        self._duration = Histogram(
            "demo_http_request_duration_seconds",
            "HTTP request duration in seconds.",
            METRIC_LABEL_POLICY["demo_http_request_duration_seconds"],
            buckets=_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self._in_progress = Gauge(
            "demo_http_requests_in_progress",
            "HTTP requests currently being processed.",
            METRIC_LABEL_POLICY["demo_http_requests_in_progress"],
            registry=self.registry,
        )
        self._fault_enabled = Gauge(
            "demo_fault_enabled",
            "Whether an allowlisted deterministic demo fault is enabled.",
            METRIC_LABEL_POLICY["demo_fault_enabled"],
            registry=self.registry,
        )
        self._adapter_calls = Counter(
            "investigator_adapter_calls_total",
            "Completed fixed-template evidence adapter calls.",
            METRIC_LABEL_POLICY["investigator_adapter_calls_total"],
            registry=self.registry,
        )
        self._adapter_duration = Histogram(
            "investigator_adapter_call_duration_seconds",
            "Duration of fixed-template evidence adapter calls.",
            METRIC_LABEL_POLICY["investigator_adapter_call_duration_seconds"],
            registry=self.registry,
        )
        self._collection_duration = Histogram(
            "investigator_evidence_collection_duration_seconds",
            "Duration of deterministic incident evidence collection.",
            METRIC_LABEL_POLICY["investigator_evidence_collection_duration_seconds"],
            registry=self.registry,
        )
        self._collection_errors = Counter(
            "investigator_evidence_collection_errors_total",
            "Explicit unavailable, failed, or timed-out evidence operations.",
            METRIC_LABEL_POLICY["investigator_evidence_collection_errors_total"],
            registry=self.registry,
        )
        self.set_slow_database_fault(enabled=False)

    def begin(self, method: str) -> None:
        """Increment the bounded in-progress saturation signal."""

        self._in_progress.labels(service=self.service_name, method=normalized_method(method)).inc()

    def finish(
        self,
        *,
        method: str,
        route: str,
        response_status: int,
        duration_seconds: float,
    ) -> None:
        """Record request traffic, errors, latency, and release in-progress state."""

        method_label = normalized_method(method)
        status_label = status_class(response_status)
        labels = {
            "service": self.service_name,
            "method": method_label,
            "route": route,
        }
        self._requests.labels(**labels, status_class=status_label).inc()
        self._duration.labels(**labels).observe(max(0.0, duration_seconds))
        if response_status >= 400:
            error_type = "client" if response_status < 500 else "server"
            self._errors.labels(**labels, error_type=error_type).inc()
        self._in_progress.labels(service=self.service_name, method=method_label).dec()

    def render(self) -> bytes:
        """Render this service's isolated registry in Prometheus format."""

        return generate_latest(self.registry)

    def set_slow_database_fault(self, enabled: bool) -> None:
        """Expose the single Stage 03 fault through fixed-cardinality labels."""

        self._fault_enabled.labels(
            service=self.service_name,
            fault="slow_database",
        ).set(1 if enabled else 0)

    def observe_adapter_call(
        self,
        *,
        source: str,
        template: str,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        """Record bounded adapter duration/outcome labels owned by repository enums."""

        self._adapter_calls.labels(
            source=source,
            template=template,
            outcome=outcome,
        ).inc()
        self._adapter_duration.labels(source=source, template=template).observe(
            max(0.0, duration_seconds)
        )
        if outcome in {"unavailable", "failed", "timed_out"}:
            self._collection_errors.labels(source=source, outcome=outcome).inc()

    def observe_evidence_collection(self, *, outcome: str, duration_seconds: float) -> None:
        """Record bounded total collection duration for a worker execution."""

        self._collection_duration.labels(outcome=outcome).observe(max(0.0, duration_seconds))


__all__ = [
    "CONTENT_TYPE_LATEST",
    "METRIC_LABEL_POLICY",
    "HttpMetrics",
    "normalized_method",
    "normalized_route",
    "status_class",
]
