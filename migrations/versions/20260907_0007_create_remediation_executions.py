"""Create remediation execution records for approval-gated rollback.

Revision ID: 20260907_0007
Revises: 20260906_0006
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260907_0007"
down_revision: str | None = "20260906_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add remediation_executions with one lifeline per recommendation."""

    op.create_table(
        "remediation_executions",
        sa.Column("id", sa.String(length=28), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("recommendation_id", sa.String(length=28), nullable=False),
        sa.Column("approval_id", sa.String(length=28), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("action_name", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("incident_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("stop_requested", sa.Boolean(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','executing','verifying','completed','failed','stopped')",
            name="ck_remediation_executions_valid_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_remediation_executions_nonnegative_attempts"),
        sa.CheckConstraint(
            "incident_version >= 1", name="ck_remediation_executions_positive_incident_version"
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_remediation_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recommendation_id"],
            ["recommendations.id"],
            name="fk_remediation_recommendation_id_recommendations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approvals.id"],
            name="fk_remediation_approval_id_approvals",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_remediation_executions"),
        sa.UniqueConstraint("recommendation_id", name="uq_remediation_recommendation"),
    )
    op.create_index(
        "ix_remediation_executions_incident_id", "remediation_executions", ["incident_id"]
    )
    op.create_index(
        "ix_remediation_executions_idempotency_key", "remediation_executions", ["idempotency_key"]
    )


def downgrade() -> None:
    """Remove remediation execution records (forward-only stages never downgrade live)."""

    op.drop_index("ix_remediation_executions_idempotency_key", table_name="remediation_executions")
    op.drop_index("ix_remediation_executions_incident_id", table_name="remediation_executions")
    op.drop_table("remediation_executions")
