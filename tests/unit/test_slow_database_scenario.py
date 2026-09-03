"""Regression coverage for scenario cleanup behavior."""

import json

import httpx
import pytest

from packages.models.deployments import DeploymentEnvironment
from scripts.scenario_slow_database import Arguments, _fault_log_summary, enabled_fault


def _arguments() -> Arguments:
    return Arguments(
        gateway_url="http://gateway.test",
        payment_url="http://payment.test",
        prometheus_url="http://prometheus.test",
        loki_url="http://loki.test",
        tempo_url="http://tempo.test",
        alertmanager_url="http://alertmanager.test",
        incident_api_url="http://incident-api.test",
        environment=DeploymentEnvironment.TEST,
        fault_control_token="scenario-token",
        traffic_count=4,
        request_timeout_seconds=5.0,
        poll_deadline_seconds=5.0,
    )


@pytest.mark.asyncio
async def test_scenario_disables_fault_when_body_fails() -> None:
    """The cleanup context sends an explicit disable even after a scenario exception."""

    requested_states: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Fault-Control-Token"] == "scenario-token"
        enabled = bool(json.loads(request.content)["enabled"])
        requested_states.append(enabled)
        return httpx.Response(
            200,
            json={
                "name": "slow_database",
                "enabled": enabled,
                "allowed": True,
                "delay_seconds": 2.5,
                "service": "payment-service",
                "service_version": "0.1.0",
                "environment": "test",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="scenario body failed"):
            async with enabled_fault(client, _arguments()):
                raise RuntimeError("scenario body failed")

    assert requested_states == [True, False]


def test_fault_log_proof_uses_emitted_event_field() -> None:
    """Loki proof matches the JSON formatter's `event` field, not a fictitious message key."""

    payload = {
        "event": "fault.slow_database.injected",
        "service": "payment-service",
        "service.version": "0.1.0",
        "deployment.environment": "test",
        "attributes": {"fault.enabled": True, "fault.name": "slow_database"},
    }

    summary = _fault_log_summary(payload, "a" * 32)

    assert summary is not None
    assert "fault.enabled=true" in summary
    assert _fault_log_summary({**payload, "event": "wrong"}, "a" * 32) is None
