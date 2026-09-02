"""Live Compose checkout integration test across all four HTTP services."""

import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from packages.models.checkout import CheckoutResponse, PaymentResponse
from packages.models.http import ErrorResponse, HealthResponse
from packages.persistence.payment_store import PaymentRow


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_checkout_chain_persists_and_replays_once() -> None:
    """Gateway → order → inventory → payment crosses HTTP and stores exactly one result."""

    gateway_url = os.getenv("TEST_GATEWAY_URL")
    payment_url = os.getenv("TEST_PAYMENT_URL")
    database_url = os.getenv("LIVE_DATABASE_URL")
    if gateway_url is None or payment_url is None or database_url is None:
        pytest.skip("live gateway, payment, and database URLs are required")

    suffix = uuid4().hex
    idempotency_key = f"integration-{suffix}"
    payload = {"customer_id": f"customer-{suffix}", "sku": "widget-001", "quantity": 2}

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        ready_response = await client.get(f"{gateway_url}/health/ready")
        assert ready_response.status_code == 200
        assert HealthResponse.model_validate(ready_response.json()).status == "ok"

        first_response = await client.post(
            f"{gateway_url}/checkout",
            json=payload,
            headers={
                "Idempotency-Key": idempotency_key,
                "X-Request-ID": f"first-{suffix}",
            },
        )
        replay_response = await client.post(
            f"{gateway_url}/checkout",
            json=payload,
            headers={
                "Idempotency-Key": idempotency_key,
                "X-Request-ID": f"replay-{suffix}",
            },
        )

        assert first_response.status_code == 200
        assert replay_response.status_code == 200
        first = CheckoutResponse.model_validate(first_response.json())
        replay = CheckoutResponse.model_validate(replay_response.json())
        assert first.request_id == f"first-{suffix}"
        assert replay.request_id == f"replay-{suffix}"
        assert first.idempotent_replay is False
        assert replay.idempotent_replay is True
        assert (replay.payment_id, replay.order_id, replay.created_at) == (
            first.payment_id,
            first.order_id,
            first.created_at,
        )

        read_response = await client.get(
            f"{payment_url}/payments/{first.payment_id}",
            headers={"X-Request-ID": f"read-{suffix}"},
        )
        assert read_response.status_code == 200
        stored = PaymentResponse.model_validate(read_response.json())
        assert stored.payment_id == first.payment_id
        assert stored.order_id == first.order_id
        assert stored.total_cents == first.total_cents

        conflict_response = await client.post(
            f"{gateway_url}/checkout",
            json={**payload, "quantity": 3},
            headers={
                "Idempotency-Key": idempotency_key,
                "X-Request-ID": f"conflict-{suffix}",
            },
        )
        assert conflict_response.status_code == 409
        assert ErrorResponse.model_validate(conflict_response.json()).code.endswith(
            "idempotency_conflict"
        )

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            row_count = await connection.scalar(
                select(func.count())
                .select_from(PaymentRow)
                .where(PaymentRow.idempotency_key == idempotency_key)
            )
        assert row_count == 1
    finally:
        await engine.dispose()
