"""Create human approval decisions and widen recommendation states.

Revision ID: 20260906_0006
Revises: 20260905_0005
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260906_0006"
down_revision: str | None = "20260905_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add approvals with one-decision-per-recommendation and approved/rejected states."""

    op.drop_constraint("ck_recommendations_valid_status", "recommendations", type_="check")
    op.create_check_constraint(
        "ck_recommendations_valid_status",
        "recommendations",
        "status IN ('proposed','waiting_for_approval','approved','rejected')",
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=28), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("recommendation_id", sa.String(length=28), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_id", sa.String(length=28), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("incident_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved','rejected')",
            name="ck_approvals_valid_decision",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_approvals_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recommendation_id"],
            ["recommendations.id"],
            name="fk_approvals_recommendation_id_recommendations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["investigation_runs.id"],
            name="fk_approvals_run_id_investigation_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["incident_reports.id"],
            name="fk_approvals_report_id_incident_reports",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.UniqueConstraint("recommendation_id", name="uq_approvals_recommendation_id"),
    )
    op.create_index("ix_approvals_incident_id", "approvals", ["incident_id"])
    op.create_index("ix_approvals_run_id", "approvals", ["run_id"])
    op.create_index("ix_approvals_idempotency_key", "approvals", ["idempotency_key"])


def downgrade() -> None:
    """Remove approvals and restore the pre-approval recommendation contract."""

    op.drop_index("ix_approvals_idempotency_key", table_name="approvals")
    op.drop_index("ix_approvals_run_id", table_name="approvals")
    op.drop_index("ix_approvals_incident_id", table_name="approvals")
    op.drop_table("approvals")
    # Recorded decisions do not survive a downgrade: map decided rows back to
    # the pre-approval pause state before restoring the narrower constraint.
    op.execute(
        "UPDATE recommendations SET status = 'waiting_for_approval' "
        "WHERE status IN ('approved','rejected')"
    )
    op.drop_constraint("ck_recommendations_valid_status", "recommendations", type_="check")
    op.create_check_constraint(
        "ck_recommendations_valid_status",
        "recommendations",
        "status IN ('proposed','waiting_for_approval')",
    )
