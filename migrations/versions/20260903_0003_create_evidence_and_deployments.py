"""Create canonical evidence and local deployment storage.

Revision ID: 20260903_0003
Revises: 20260902_0002
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260903_0003"
down_revision: str | None = "20260902_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Stage 3 evidence, deployment, and investigation-run states."""

    op.drop_constraint(
        "ck_investigation_runs_valid_stage",
        "investigation_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_investigation_runs_valid_status",
        "investigation_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_investigation_runs_valid_stage",
        "investigation_runs",
        "stage IN ('no_ai_placeholder','evidence_collection')",
    )
    op.create_check_constraint(
        "ck_investigation_runs_valid_status",
        "investigation_runs",
        "status IN ('queued','running','placeholder_complete_no_ai','evidence_collected',"
        "'retry_scheduled','failed','dead_lettered','skipped_terminal')",
    )

    op.create_table(
        "deployments",
        sa.Column("id", sa.String(length=24), nullable=False),
        sa.Column("service", sa.String(length=128), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column(
            "changed_files",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "deployment_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deployments"),
        sa.UniqueConstraint(
            "service",
            "environment",
            "version",
            "deployed_at",
            "commit_sha",
            name="uq_deployments_identity",
        ),
    )
    op.create_index(
        "ix_deployments_service_environment_time",
        "deployments",
        ["service", "environment", "deployed_at"],
    )

    op.create_table(
        "evidence",
        sa.Column("id", sa.String(length=28), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("evidence_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.String(length=512), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("query_template", sa.String(length=64), nullable=False),
        sa.Column(
            "query_parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('prometheus','loki','tempo','deployment_store')",
            name="ck_evidence_valid_source",
        ),
        sa.CheckConstraint(
            "evidence_type IN ('metric','log','trace','deployment')",
            name="ck_evidence_valid_type",
        ),
        sa.CheckConstraint(
            "status IN ('collected','empty','unavailable','failed','timed_out')",
            name="ck_evidence_valid_status",
        ),
        sa.CheckConstraint("window_end >= window_start", name="ck_evidence_valid_window"),
        sa.CheckConstraint(
            "((status IN ('unavailable','failed','timed_out')) AND error_type IS NOT NULL "
            "AND error_message IS NOT NULL) OR "
            "((status IN ('collected','empty')) AND error_type IS NULL "
            "AND error_message IS NULL)",
            name="ck_evidence_valid_error_state",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_evidence_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evidence"),
    )
    op.create_index("ix_evidence_incident_id", "evidence", ["incident_id"])
    op.create_index("ix_evidence_source", "evidence", ["source"])
    op.create_index("ix_evidence_status", "evidence", ["status"])
    op.create_index(
        "ix_evidence_incident_observed",
        "evidence",
        ["incident_id", "observed_at", "id"],
    )


def downgrade() -> None:
    """Remove Stage 3 storage and restore the Stage 04 run-state contract."""

    op.drop_index("ix_evidence_incident_observed", table_name="evidence")
    op.drop_index("ix_evidence_status", table_name="evidence")
    op.drop_index("ix_evidence_source", table_name="evidence")
    op.drop_index("ix_evidence_incident_id", table_name="evidence")
    op.drop_table("evidence")
    op.drop_index("ix_deployments_service_environment_time", table_name="deployments")
    op.drop_table("deployments")

    op.drop_constraint(
        "ck_investigation_runs_valid_stage",
        "investigation_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_investigation_runs_valid_status",
        "investigation_runs",
        type_="check",
    )
    op.execute(
        "UPDATE investigation_runs SET stage = 'no_ai_placeholder', "
        "status = CASE WHEN status = 'evidence_collected' "
        "THEN 'placeholder_complete_no_ai' ELSE status END "
        "WHERE stage = 'evidence_collection'"
    )
    op.create_check_constraint(
        "ck_investigation_runs_valid_stage",
        "investigation_runs",
        "stage = 'no_ai_placeholder'",
    )
    op.create_check_constraint(
        "ck_investigation_runs_valid_status",
        "investigation_runs",
        "status IN ('queued','running','placeholder_complete_no_ai','retry_scheduled',"
        "'failed','dead_lettered','skipped_terminal')",
    )
