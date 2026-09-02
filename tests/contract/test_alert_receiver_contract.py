"""In-process API contract for the disposable Alertmanager receiver."""

import httpx
import pytest

from apps.demo.alert_receiver.main import create_app
from packages.config import Settings
from packages.models.alerts import AlertDeliveryList, AlertReceiptResponse

pytestmark = pytest.mark.contract


def _payload(status: str = "firing") -> dict[str, object]:
    return {
        "version": "4",
        "status": status,
        "receiver": "stage03-webhook",
        "groupKey": '{}:{alertname="DemoPaymentHighLatency"}',
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": "DemoPaymentHighLatency",
                    "service": "payment-service",
                    "severity": "warning",
                },
                "annotations": {"summary": "payment is slow"},
                "startsAt": "2026-09-02T12:00:00Z",
                "endsAt": "2026-09-02T12:05:00Z",
                "generatorURL": "http://prometheus.test/graph",
                "fingerprint": "fixed-fingerprint",
            }
        ],
    }


@pytest.mark.asyncio
async def test_receiver_accepts_filters_and_clears_bounded_delivery() -> None:
    """The stub returns 202 and exposes deterministic scenario-only readback."""

    app = create_app(Settings(_env_file=None, environment="test", telemetry_enabled=False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://receiver.test",
    ) as client:
        ready = await client.get("/health/ready")
        accepted = await client.post("/webhooks/alertmanager", json=_payload())
        deliveries = await client.get(
            "/deliveries",
            params={"alertname": "DemoPaymentHighLatency", "status": "firing"},
        )
        unrelated = await client.get(
            "/deliveries",
            params={"alertname": "OtherAlert"},
        )
        cleared = await client.delete("/deliveries")
        after_clear = await client.get("/deliveries")

    assert ready.status_code == 200
    assert accepted.status_code == 202
    assert AlertReceiptResponse.model_validate(accepted.json()).sequence == 1
    assert len(AlertDeliveryList.model_validate(deliveries.json()).deliveries) == 1
    assert AlertDeliveryList.model_validate(unrelated.json()).deliveries == []
    assert cleared.json() == {"cleared": 1}
    assert AlertDeliveryList.model_validate(after_clear.json()).deliveries == []
