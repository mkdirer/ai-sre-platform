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


def test_alertmanager_routes_firing_and_resolved_to_stub() -> None:
    """The disposable receiver gets both edges of the alert lifecycle."""

    configuration = Path("observability/alertmanager/alertmanager.yml").read_text()

    assert "http://alert-receiver:8000/webhooks/alertmanager" in configuration
    assert "send_resolved: true" in configuration
    assert "group_wait: 1s" in configuration
