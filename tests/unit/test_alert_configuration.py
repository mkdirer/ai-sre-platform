"""Static assertions for the repository-owned alert path configuration."""

from pathlib import Path


def test_payment_latency_alert_uses_real_bounded_metric_labels() -> None:
    """The alert selects the Stage 02 histogram using only emitted stable labels."""

    rule = Path("observability/prometheus/rules/demo-alerts.yml").read_text()

    assert "DemoPaymentHighLatency" in rule
    assert "demo_http_request_duration_seconds_bucket" in rule
    assert 'service="payment-service"' in rule
    assert 'method="POST"' in rule
    assert 'route="/payments"' in rule
    assert "for: 5s" in rule
    assert "request_id" not in rule
    assert "trace_id" not in rule


def test_alertmanager_routes_firing_and_resolved_to_durable_incident_api() -> None:
    """Stage 04 replaces the runtime stub route while preserving both lifecycle edges."""

    configuration = Path("observability/alertmanager/alertmanager.yml").read_text()

    assert "http://incident-api:8000/api/v1/alerts" in configuration
    assert "send_resolved: true" in configuration
    assert "group_wait: 1s" in configuration


def test_disposable_receiver_remains_an_explicit_test_tool() -> None:
    """The Milestone 1 receiver keeps contract-test value but is absent from normal startup."""

    compose = Path("docker-compose.yml").read_text()

    assert "alert-receiver:" in compose
    assert 'profiles: ["test-tools"]' in compose
