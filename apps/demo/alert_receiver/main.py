"""Disposable in-memory webhook receiver for Milestone 1C verification."""

from datetime import UTC, datetime
from threading import RLock
from typing import Annotated

from fastapi import FastAPI, Query, Response, status

from apps.demo.common.web import create_service_app, get_telemetry
from packages.config import Settings
from packages.models.alerts import (
    AlertClearResponse,
    AlertDelivery,
    AlertDeliveryList,
    AlertmanagerWebhook,
    AlertReceiptResponse,
    AlertStatus,
)
from packages.models.http import HealthResponse


class DeliveryStore:
    """Process-local, bounded, concurrency-safe delivery history."""

    _MAX_DELIVERIES = 100

    def __init__(self) -> None:
        self._lock = RLock()
        self._deliveries: list[AlertDelivery] = []
        self._next_sequence = 1

    def add(self, webhook: AlertmanagerWebhook) -> AlertDelivery:
        with self._lock:
            delivery = AlertDelivery(
                sequence=self._next_sequence,
                received_at=datetime.now(UTC),
                status=webhook.status,
                receiver=webhook.receiver,
                alerts=webhook.alerts,
            )
            self._next_sequence += 1
            self._deliveries.append(delivery)
            del self._deliveries[: -self._MAX_DELIVERIES]
            return delivery

    def list(
        self,
        *,
        alertname: str | None = None,
        delivery_status: AlertStatus | None = None,
    ) -> list[AlertDelivery]:
        with self._lock:
            deliveries = list(self._deliveries)
        return [
            delivery
            for delivery in deliveries
            if (delivery_status is None or delivery.status == delivery_status)
            and (
                alertname is None
                or any(alert.labels.get("alertname") == alertname for alert in delivery.alerts)
            )
        ]

    def clear(self) -> int:
        with self._lock:
            cleared = len(self._deliveries)
            self._deliveries.clear()
        return cleared


def create_app(
    settings: Settings | None = None,
    *,
    store: DeliveryStore | None = None,
) -> FastAPI:
    """Build the dependency-free local receiver stub."""

    resolved_settings = settings or Settings()
    resolved_store = store or DeliveryStore()
    app = create_service_app(
        title="AI SRE Demo Alert Receiver Stub",
        service_name="alert-receiver",
        settings=resolved_settings,
    )
    telemetry = get_telemetry(app)

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(service="alert-receiver")

    @app.get("/health/ready", response_model=HealthResponse)
    async def readiness() -> HealthResponse:
        return HealthResponse(service="alert-receiver")

    @app.post(
        "/webhooks/alertmanager",
        response_model=AlertReceiptResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def receive_alert(webhook: AlertmanagerWebhook) -> AlertReceiptResponse:
        delivery = resolved_store.add(webhook)
        alert_names = sorted(
            {alert.labels.get("alertname", "unknown") for alert in delivery.alerts}
        )
        telemetry.logger.info(
            "alertmanager.webhook.received",
            extra={
                "structured": {
                    "alert.status": delivery.status,
                    "alert.names": alert_names,
                    "alert.count": len(delivery.alerts),
                    "delivery.sequence": delivery.sequence,
                }
            },
        )
        return AlertReceiptResponse(sequence=delivery.sequence)

    @app.get("/deliveries", response_model=AlertDeliveryList)
    async def list_deliveries(
        alertname: Annotated[
            str | None,
            Query(min_length=1, max_length=128),
        ] = None,
        delivery_status: Annotated[
            AlertStatus | None,
            Query(alias="status"),
        ] = None,
    ) -> AlertDeliveryList:
        return AlertDeliveryList(
            deliveries=resolved_store.list(
                alertname=alertname,
                delivery_status=delivery_status,
            )
        )

    @app.delete("/deliveries", response_model=AlertClearResponse)
    async def clear_deliveries(response: Response) -> AlertClearResponse:
        response.headers["Cache-Control"] = "no-store"
        return AlertClearResponse(cleared=resolved_store.clear())

    return app
