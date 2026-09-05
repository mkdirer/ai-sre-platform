"""Unit coverage for correlation, redaction, metrics, and disabled telemetry."""

import json
import logging
import re

import httpx
import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from apps.demo.common.web import TRACE_ID_HEADER, get_telemetry
from apps.demo.gateway.main import create_app as create_gateway_app
from packages.config import Settings
from packages.telemetry import (
    METRIC_LABEL_POLICY,
    JsonLogFormatter,
    TelemetryRuntime,
    bind_request_id,
    extract_trace_context,
    inject_trace_context,
    redact_text,
    redact_value,
    reset_request_id,
)
from packages.telemetry.metrics import HttpMetrics, normalized_method, normalized_route
from tests.fakes import FakeOrderClient

TRACE_ID = 0x1234567890ABCDEF1234567890ABCDEF
SPAN_ID = 0x1234567890ABCDEF


def _test_span() -> NonRecordingSpan:
    return NonRecordingSpan(
        SpanContext(
            trace_id=TRACE_ID,
            span_id=SPAN_ID,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
    )


def test_w3c_trace_context_round_trip() -> None:
    """The propagation helper emits and extracts one standards-compliant traceparent."""

    token = otel_context.attach(trace.set_span_in_context(_test_span()))
    try:
        headers: dict[str, str] = {}
        inject_trace_context(headers)
    finally:
        otel_context.detach(token)

    assert headers == {"traceparent": "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"}
    extracted = trace.get_current_span(extract_trace_context(headers)).get_span_context()
    assert extracted.trace_id == TRACE_ID
    assert extracted.span_id == SPAN_ID
    assert extracted.is_remote is True


def test_json_log_enrichment_and_recursive_redaction() -> None:
    """Structured logs correlate safely without exposing common credential fields."""

    formatter = JsonLogFormatter(
        service_name="gateway",
        service_version="0.1.0",
        environment="test",
    )
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="checkout token=message-secret Bearer bearer-secret",
        args=(),
        exc_info=None,
    )
    record.structured = {
        "authorization": "Bearer structured-secret",
        "nested": {"password": "database-secret", "safe": "visible"},
    }
    request_token = bind_request_id("request-123")
    trace_token = otel_context.attach(trace.set_span_in_context(_test_span()))
    try:
        payload = json.loads(formatter.format(record))
    finally:
        otel_context.detach(trace_token)
        reset_request_id(request_token)

    serialized = json.dumps(payload)
    assert "message-secret" not in serialized
    assert "bearer-secret" not in serialized
    assert "structured-secret" not in serialized
    assert "database-secret" not in serialized
    assert payload["severity"] == "WARNING"
    assert payload["service"] == payload["service.name"] == "gateway"
    assert payload["service.version"] == "0.1.0"
    assert payload["deployment.environment"] == "test"
    assert payload["trace_id"] == "1234567890abcdef1234567890abcdef"
    assert payload["span_id"] == "1234567890abcdef"
    assert payload["request_id"] == "request-123"
    assert payload["attributes"]["nested"]["safe"] == "visible"


def test_hot_path_redaction_covers_compound_keys_json_and_urls() -> None:
    """Regression: client_secret/db_password/pwd, quoted JSON keys, and URL userinfo."""

    assert redact_value({"client_secret": "hunter2"}) == {"client_secret": "[REDACTED]"}
    assert redact_value({"nested": {"db_password": "x", "safe": "visible"}}) == {
        "nested": {"db_password": "[REDACTED]", "safe": "visible"}
    }
    assert redact_value({"pwd": "hunter2"}) == {"pwd": "[REDACTED]"}

    out = redact_text('{"db_password": "change-me", "token": "s3cr3t", "ok": true}')
    assert "change-me" not in out
    assert "s3cr3t" not in out
    assert '"ok": true' in out

    out = redact_text("db=postgres://aisre:p@ss@db:5432/aisre ok")
    assert "p@ss" not in out
    assert "postgres://aisre:[REDACTED]@db:5432/aisre ok" in out

    out = redact_text("db=postgres://u:p/a@ss@h/db ok")
    assert "p/a@ss" not in out
    assert "postgres://u:[REDACTED]@h/db ok" in out

    assert redact_text("pwd=hunter2") == "pwd=[REDACTED]"
    benign = "benign eval text | SCN-001 | true |"
    assert redact_text(benign) == benign


def test_metric_labels_and_route_policy_are_bounded() -> None:
    """Metrics never use caller IDs, raw paths, arbitrary methods, or payload fields."""

    all_labels = {label for labels in METRIC_LABEL_POLICY.values() for label in labels}
    assert all_labels.isdisjoint(
        {"request_id", "trace_id", "customer_id", "payment_id", "idempotency_key"}
    )
    assert normalized_method("caller-defined-method") == "OTHER"
    assert normalized_route({"path": "/payments/caller-controlled-id"}) == "_unmatched"

    metrics = HttpMetrics("payment-service")
    metrics.begin("POST")
    metrics.finish(
        method="POST",
        route="/payments",
        response_status=503,
        duration_seconds=0.25,
    )
    exposition = metrics.render().decode()
    assert 'route="/payments"' in exposition
    assert 'error_type="server"' in exposition
    assert "caller-controlled-id" not in exposition


@pytest.mark.asyncio
async def test_telemetry_disabled_keeps_checkout_and_pull_metrics_working() -> None:
    """Disabling OTLP exporters has no effect on checkout or local pull metrics."""

    settings = Settings(_env_file=None, environment="test", telemetry_enabled=False)
    runtime = TelemetryRuntime.create(service_name="gateway", settings=settings)
    assert runtime.enabled is False
    assert runtime.tracer_provider is None
    assert runtime.logger_provider is None

    app = create_gateway_app(settings, order_client=FakeOrderClient())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    ) as client:
        checkout = await client.post(
            "/checkout",
            json={"customer_id": "customer-1", "sku": "widget-001", "quantity": 1},
            headers={"Idempotency-Key": "telemetry-disabled"},
        )
        metrics = await client.get("/metrics")

    assert checkout.status_code == 200
    assert TRACE_ID_HEADER not in checkout.headers
    assert metrics.status_code == 200
    assert 'route="/checkout"' in metrics.text


@pytest.mark.asyncio
async def test_unreachable_otlp_exporter_does_not_break_checkout() -> None:
    """Exporter connection failure remains outside the synchronous request result."""

    settings = Settings(
        _env_file=None,
        environment="test",
        telemetry_enabled=True,
        otel_exporter_otlp_endpoint="http://127.0.0.1:1",
        otel_export_timeout_seconds=0.1,
        otel_batch_schedule_delay_milliseconds=100,
    )
    app = create_gateway_app(settings, order_client=FakeOrderClient())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
        ) as client:
            response = await client.post(
                "/checkout",
                json={"customer_id": "customer-1", "sku": "widget-001", "quantity": 1},
                headers={"Idempotency-Key": "exporter-unavailable"},
            )
    finally:
        get_telemetry(app).shutdown()

    assert response.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers[TRACE_ID_HEADER])
