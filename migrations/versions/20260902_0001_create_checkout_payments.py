"""Create durable checkout payment records.

Revision ID: 20260902_0001
Revises:
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260902_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the first application table from an empty database."""

    op.create_table(
        "checkout_payments",
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("quantity > 0", name="quantity_positive"),
        sa.CheckConstraint(
            "status = 'confirmed'",
            name="status_confirmed",
        ),
        sa.CheckConstraint(
            "total_cents > 0",
            name="total_positive",
        ),
        sa.CheckConstraint(
            "unit_price_cents > 0",
            name="unit_price_positive",
        ),
        sa.PrimaryKeyConstraint("payment_id", name="pk_checkout_payments"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_checkout_payments_idempotency_key",
        ),
        sa.UniqueConstraint("order_id", name="uq_checkout_payments_order_id"),
    )


def downgrade() -> None:
    """Remove the Stage 01 table."""

    op.drop_table("checkout_payments")
