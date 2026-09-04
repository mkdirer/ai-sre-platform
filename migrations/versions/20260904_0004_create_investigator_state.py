"""Create durable evidence-grounded investigator state.

Revision ID: 20260904_0004
Revises: 20260903_0003
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260904_0004"
down_revision: str | None = "20260903_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Stage 06 run states, artifacts, audit metadata, and LangGraph checkpoints."""

    op.drop_constraint("ck_investigation_runs_valid_stage", "investigation_runs", type_="check")
    op.drop_constraint("ck_investigation_runs_valid_status", "investigation_runs", type_="check")
    op.create_check_constraint(
        "ck_investigation_runs_valid_stage",
        "investigation_runs",
        "stage IN ('no_ai_placeholder','evidence_collection','ai_investigation')",
    )
    op.create_check_constraint(
        "ck_investigation_runs_valid_status",
        "investigation_runs",
        "status IN ('queued','running','placeholder_complete_no_ai','evidence_collected',"
        "'report_generated','waiting_for_approval','insufficient_evidence','retry_scheduled',"
        "'failed','dead_lettered','skipped_terminal')",
    )

    op.create_table(
        "hypotheses",
        sa.Column("id", sa.String(length=28), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("supporting_evidence_ids", postgresql.ARRAY(sa.String(28)), nullable=False),
        sa.Column("contradicting_evidence_ids", postgresql.ARRAY(sa.String(28)), nullable=False),
        sa.Column(
            "reasoning_summary",
            sa.String(length=512),
            nullable=False,
        ),
        sa.Column(
            "next_evidence_requests",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed','verified','rejected','inconclusive')",
            name="ck_hypotheses_valid_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_hypotheses_valid_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_hypotheses_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["investigation_runs.id"],
            name="fk_hypotheses_run_id_investigation_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hypotheses"),
    )
    op.create_index("ix_hypotheses_incident_id", "hypotheses", ["incident_id"])
    op.create_index("ix_hypotheses_run_id", "hypotheses", ["run_id"])

    op.create_table(
        "incident_reports",
        sa.Column("id", sa.String(length=28), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("root_cause", sa.String(length=64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('complete','insufficient_evidence','waiting_for_approval')",
            name="ck_incident_reports_valid_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_incident_reports_valid_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_incident_reports_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["investigation_runs.id"],
            name="fk_incident_reports_run_id_investigation_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_incident_reports"),
        sa.UniqueConstraint("run_id", name="uq_incident_reports_run_id"),
    )
    op.create_index("ix_incident_reports_incident_id", "incident_reports", ["incident_id"])

    op.create_table(
        "recommendations",
        sa.Column("id", sa.String(length=28), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_id", sa.String(length=28), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rationale_evidence_ids", postgresql.ARRAY(sa.String(28)), nullable=False),
        sa.Column("risk", sa.String(length=16), nullable=False),
        sa.Column("reversible", sa.Boolean(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed','waiting_for_approval')",
            name="ck_recommendations_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_recommendations_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["investigation_runs.id"],
            name="fk_recommendations_run_id_investigation_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["incident_reports.id"],
            name="fk_recommendations_report_id_incident_reports",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recommendations"),
    )
    op.create_index("ix_recommendations_incident_id", "recommendations", ["incident_id"])
    op.create_index("ix_recommendations_run_id", "recommendations", ["run_id"])
    op.create_index("ix_recommendations_report_id", "recommendations", ["report_id"])

    op.create_table(
        "investigator_calls",
        sa.Column("id", sa.String(length=29), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("call_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('model','tool')", name="ck_investigator_calls_valid_kind"),
        sa.CheckConstraint(
            "status IN ('succeeded','failed','rejected')",
            name="ck_investigator_calls_valid_status",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_investigator_calls_positive_attempt"),
        sa.CheckConstraint(
            "duration_seconds >= 0",
            name="ck_investigator_calls_nonnegative_duration",
        ),
        sa.CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="ck_investigator_calls_nonnegative_cost",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_investigator_calls_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["investigation_runs.id"],
            name="fk_investigator_calls_run_id_investigation_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_investigator_calls"),
    )
    op.create_index("ix_investigator_calls_incident_id", "investigator_calls", ["incident_id"])
    op.create_index("ix_investigator_calls_run_id", "investigator_calls", ["run_id"])

    op.create_table(
        "investigation_failures",
        sa.Column("id", sa.String(length=29), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("error_type", sa.String(length=128), nullable=False),
        sa.Column("error_message", sa.String(length=512), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_investigation_failures_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["investigation_runs.id"],
            name="fk_investigation_failures_run_id_investigation_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_failures"),
    )
    op.create_index(
        "ix_investigation_failures_incident_id", "investigation_failures", ["incident_id"]
    )
    op.create_index("ix_investigation_failures_run_id", "investigation_failures", ["run_id"])

    _create_langgraph_checkpoint_tables()


def _create_langgraph_checkpoint_tables() -> None:
    """Create the schema expected by langgraph-checkpoint-postgres 3.1.2."""

    op.create_table(
        "checkpoint_migrations",
        sa.Column("v", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("v"),
    )
    op.bulk_insert(
        sa.table("checkpoint_migrations", sa.column("v", sa.Integer())),
        [{"v": version} for version in range(10)],
    )
    op.create_table(
        "checkpoints",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False, server_default=""),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("parent_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id"),
    )
    op.create_index("ix_checkpoints_thread_id", "checkpoints", ["thread_id"])
    op.create_table(
        "checkpoint_blobs",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False, server_default=""),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("blob", sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "channel", "version"),
    )
    op.create_index("ix_checkpoint_blobs_thread_id", "checkpoint_blobs", ["thread_id"])
    op.create_table(
        "checkpoint_writes",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False, server_default=""),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("blob", sa.LargeBinary(), nullable=False),
        sa.Column("task_path", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
    )
    op.create_index("ix_checkpoint_writes_thread_id", "checkpoint_writes", ["thread_id"])


def downgrade() -> None:
    """Remove Stage 06 state and restore the Stage 05 run-state contract."""

    op.drop_index("ix_checkpoint_writes_thread_id", table_name="checkpoint_writes")
    op.drop_table("checkpoint_writes")
    op.drop_index("ix_checkpoint_blobs_thread_id", table_name="checkpoint_blobs")
    op.drop_table("checkpoint_blobs")
    op.drop_index("ix_checkpoints_thread_id", table_name="checkpoints")
    op.drop_table("checkpoints")
    op.drop_table("checkpoint_migrations")
    op.drop_index("ix_investigation_failures_run_id", table_name="investigation_failures")
    op.drop_index("ix_investigation_failures_incident_id", table_name="investigation_failures")
    op.drop_table("investigation_failures")
    op.drop_index("ix_investigator_calls_run_id", table_name="investigator_calls")
    op.drop_index("ix_investigator_calls_incident_id", table_name="investigator_calls")
    op.drop_table("investigator_calls")
    op.drop_index("ix_recommendations_report_id", table_name="recommendations")
    op.drop_index("ix_recommendations_run_id", table_name="recommendations")
    op.drop_index("ix_recommendations_incident_id", table_name="recommendations")
    op.drop_table("recommendations")
    op.drop_index("ix_incident_reports_incident_id", table_name="incident_reports")
    op.drop_table("incident_reports")
    op.drop_index("ix_hypotheses_run_id", table_name="hypotheses")
    op.drop_index("ix_hypotheses_incident_id", table_name="hypotheses")
    op.drop_table("hypotheses")

    op.drop_constraint("ck_investigation_runs_valid_stage", "investigation_runs", type_="check")
    op.drop_constraint("ck_investigation_runs_valid_status", "investigation_runs", type_="check")
    op.execute(
        "UPDATE investigation_runs SET stage = 'evidence_collection', "
        "status = CASE WHEN status IN ('report_generated','waiting_for_approval',"
        "'insufficient_evidence') THEN 'evidence_collected' ELSE status END "
        "WHERE stage = 'ai_investigation'"
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
