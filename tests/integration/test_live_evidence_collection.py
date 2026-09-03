"""Live Stage 05 proof from slow-database traffic to Incident API evidence."""

import os

import httpx
import pytest

from packages.models.deployments import DeploymentEnvironment
from scripts.scenario_slow_database import Arguments, run_scenario


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slow_database_collects_real_evidence_through_incident_api() -> None:
    """Exercise all local sources through the production alert and worker path."""

    if os.getenv("RUN_LOCAL_EVIDENCE_INTEGRATION") != "true":
        pytest.skip("RUN_LOCAL_EVIDENCE_INTEGRATION=true is required for the bounded fault test")

    prometheus_url = os.getenv("TEST_PROMETHEUS_URL", "http://127.0.0.1:9090")
    await run_scenario(
        Arguments(
            gateway_url=os.getenv("TEST_GATEWAY_URL", "http://127.0.0.1:8001"),
            payment_url=os.getenv("TEST_PAYMENT_URL", "http://127.0.0.1:8004"),
            prometheus_url=prometheus_url,
            loki_url=os.getenv("TEST_LOKI_URL", "http://127.0.0.1:3100"),
            tempo_url=os.getenv("TEST_TEMPO_URL", "http://127.0.0.1:3200"),
            alertmanager_url=os.getenv("TEST_ALERTMANAGER_URL", "http://127.0.0.1:9093"),
            incident_api_url=os.getenv("TEST_INCIDENT_API_URL", "http://127.0.0.1:8006"),
            environment=DeploymentEnvironment(
                os.getenv("ENVIRONMENT", DeploymentEnvironment.DEVELOPMENT.value)
            ),
            fault_control_token=os.getenv("FAULT_CONTROL_TOKEN", "local-demo-fault-control"),
            traffic_count=4,
            request_timeout_seconds=10.0,
            poll_deadline_seconds=90.0,
        )
    )

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(
            os.getenv(
                "TEST_INVESTIGATOR_METRICS_URL",
                "http://127.0.0.1:9464/metrics",
            )
        )
        scrape_response = await client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": 'sum(investigator_adapter_calls_total{job="investigator-worker"})'},
        )
    response.raise_for_status()
    scrape_response.raise_for_status()
    metrics = response.text
    assert "investigator_adapter_calls_total" in metrics
    assert 'source="prometheus",template="metric.service_latency_p95"' in metrics
    assert "investigator_evidence_collection_duration_seconds_count" in metrics
    scrape_results = scrape_response.json()["data"]["result"]
    assert scrape_results
    assert float(scrape_results[0]["value"][1]) >= 12
