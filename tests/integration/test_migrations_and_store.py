"""PostgreSQL migration and durable idempotency integration tests."""

import asyncio
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, select
from sqlalchemy.ext.asyncio import create_async_engine

from packages.config import Settings
from packages.models.checkout import PaymentRequest
from packages.persistence import IdempotencyConflict, SqlAlchemyPaymentStore
from packages.persistence.payment_store import PaymentRow


def test_migration_graph_has_one_current_head() -> None:
    """Stage 01 exposes one unambiguous migration head."""

    script = ScriptDirectory.from_config(Config("alembic.ini"))

    assert script.get_heads() == ["20260902_0001"]


async def _inspect_schema(database_url: str) -> dict[str, Any]:
    engine = create_async_engine(database_url)

    def inspect_connection(connection: Connection) -> dict[str, Any]:
        inspector = inspect(connection)
        return {
            "tables": inspector.get_table_names(),
            "checks": inspector.get_check_constraints("checkout_payments"),
            "uniques": inspector.get_unique_constraints("checkout_payments"),
        }

    try:
        async with engine.connect() as connection:
            return await connection.run_sync(inspect_connection)
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_migration_applies_to_empty_database_and_is_repeatable(
    migrated_test_database_url: str,
) -> None:
    """The first migration creates the required constraints and can be reapplied at head."""

    schema = asyncio.run(_inspect_schema(migrated_test_database_url))
    unique_names = {constraint["name"] for constraint in schema["uniques"]}
    check_names = {constraint["name"] for constraint in schema["checks"]}

    assert "checkout_payments" in schema["tables"]
    assert unique_names == {
        "uq_checkout_payments_idempotency_key",
        "uq_checkout_payments_order_id",
    }
    assert check_names == {
        "ck_checkout_payments_quantity_positive",
        "ck_checkout_payments_status_confirmed",
        "ck_checkout_payments_total_positive",
        "ck_checkout_payments_unit_price_positive",
    }

    config = Config("alembic.ini")
    config.attributes["database_url"] = migrated_test_database_url
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_payment_store_retry_returns_one_durable_record(
    migrated_test_database_url: str,
) -> None:
    """Exact retries reuse one row while mismatched key reuse is rejected."""

    engine = create_async_engine(migrated_test_database_url)
    store = SqlAlchemyPaymentStore(Settings(_env_file=None), engine=engine)
    payment_request = PaymentRequest(
        order_id=UUID("14af0742-8cf1-44d5-ab4b-a84ff94a8ea8"),
        reservation_id=UUID("1c98b449-76e8-49ca-8e21-2ec33c7c8ac5"),
        customer_id="integration-customer",
        sku="widget-001",
        quantity=2,
        unit_price_cents=1999,
    )

    try:
        first = await store.create_or_get(payment_request, idempotency_key="store-retry-1")
        replay = await store.create_or_get(payment_request, idempotency_key="store-retry-1")

        assert replay.payment_id == first.payment_id
        assert first.idempotent_replay is False
        assert replay.idempotent_replay is True

        with pytest.raises(IdempotencyConflict):
            await store.create_or_get(
                payment_request.model_copy(update={"quantity": 3}),
                idempotency_key="store-retry-1",
            )

        with pytest.raises(IdempotencyConflict):
            await store.create_or_get(payment_request, idempotency_key="different-key-same-order")

        concurrent_request = payment_request.model_copy(
            update={
                "order_id": UUID("c2e4e960-5a17-4dce-ac1c-599891ab8c8c"),
                "reservation_id": UUID("c8999de4-ec4b-46f8-8752-868af339440b"),
            }
        )
        concurrent_results = await asyncio.gather(
            store.create_or_get(concurrent_request, idempotency_key="store-concurrent-1"),
            store.create_or_get(concurrent_request, idempotency_key="store-concurrent-1"),
        )
        assert concurrent_results[0].payment_id == concurrent_results[1].payment_id
        assert sorted(result.idempotent_replay for result in concurrent_results) == [False, True]

        async with engine.connect() as connection:
            retry_rows = (
                await connection.execute(
                    select(PaymentRow).where(PaymentRow.idempotency_key == "store-retry-1")
                )
            ).scalars()
            concurrent_rows = (
                await connection.execute(
                    select(PaymentRow).where(PaymentRow.idempotency_key == "store-concurrent-1")
                )
            ).scalars()
            assert len(list(retry_rows)) == 1
            assert len(list(concurrent_rows)) == 1
    finally:
        await store.close()
