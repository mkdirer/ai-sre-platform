"""Create durable incident, occurrence, investigation, queue, and audit state.

Revision ID: 20260902_0002
Revises: 20260902_0001
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260902_0002"
down_revision: str | None = "20260902_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the complete Stage 04 durable ingestion and queue schema."""

    op.create_table(
        "incidents",
        sa.Column("id", sa.String(length=20), nullable=False),
        sa.Column("alert_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("alert_name", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("service", sa.String(length=128), nullable=False),
        sa.Column("affected_services", postgresql.ARRAY(sa.String(length=128)), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("investigation_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("investigation_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
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
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_incidents_valid_confidence",
        ),
        sa.CheckConstraint("version >= 1", name="ck_incidents_positive_version"),
        sa.CheckConstraint(
            "severity IN ('info','warning','critical')",
            name="ck_incidents_valid_severity",
        ),
        sa.CheckConstraint(
            "status IN ('queued','investigating','waiting_for_approval','remediating',"
            "'verifying','resolved','insufficient_evidence','investigation_failed',"
            "'rejected','closed')",
            name="ck_incidents_valid_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_incidents"),
        sa.UniqueConstraint("alert_fingerprint", name="uq_incidents_alert_fingerprint"),
    )
    op.create_index("ix_incidents_status", "incidents", ["status"])
    op.create_index("ix_incidents_updated_at", "incidents", ["updated_at"])

    op.create_table(
        "alert_occurrences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("delivery_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("annotations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('firing','resolved')",
            name="ck_alert_occurrences_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_alert_occurrences_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alert_occurrences"),
        sa.UniqueConstraint(
            "delivery_fingerprint", name="uq_alert_occurrences_delivery_fingerprint"
        ),
    )
    op.create_index("ix_alert_occurrences_incident_id", "alert_occurrences", ["incident_id"])

    op.create_table(
        "investigation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("attempt >= 0", name="ck_investigation_runs_nonnegative_attempt"),
        sa.CheckConstraint(
            "stage = 'no_ai_placeholder'",
            name="ck_investigation_runs_valid_stage",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','placeholder_complete_no_ai','retry_scheduled',"
            "'failed','dead_lettered','skipped_terminal')",
            name="ck_investigation_runs_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_investigation_runs_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_runs"),
    )
    op.create_index("ix_investigation_runs_incident_id", "investigation_runs", ["incident_id"])

    op.create_table(
        "queue_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("celery_task_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error_type", sa.String(length=128), nullable=True),
        sa.Column("last_error_message", sa.String(length=512), nullable=True),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("attempts >= 0", name="ck_queue_jobs_nonnegative_attempts"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_queue_jobs_positive_max_attempts"),
        sa.CheckConstraint(
            "status IN ('pending_publish','queued','processing','retry_scheduled','completed',"
            "'publish_failed','dead_lettered','skipped_terminal')",
            name="ck_queue_jobs_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_queue_jobs_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id"],
            ["investigation_runs.id"],
            name="fk_queue_jobs_investigation_run_id_investigation_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_queue_jobs"),
        sa.UniqueConstraint("celery_task_id", name="uq_queue_jobs_celery_task_id"),
        sa.UniqueConstraint("investigation_run_id", name="uq_queue_jobs_investigation_run_id"),
    )
    op.create_index("ix_queue_jobs_incident_id", "queue_jobs", ["incident_id"])
    op.create_index("ix_queue_jobs_status", "queue_jobs", ["status"])

    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", sa.String(length=20), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_audit_events_incident_id_incidents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index("ix_audit_events_incident_id", "audit_events", ["incident_id"])


def downgrade() -> None:
    """Remove Stage 04 state while preserving the Stage 01 checkout table."""

    op.drop_index("ix_audit_events_incident_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_queue_jobs_status", table_name="queue_jobs")
    op.drop_index("ix_queue_jobs_incident_id", table_name="queue_jobs")
    op.drop_table("queue_jobs")
    op.drop_index("ix_investigation_runs_incident_id", table_name="investigation_runs")
    op.drop_table("investigation_runs")
    op.drop_index("ix_alert_occurrences_incident_id", table_name="alert_occurrences")
    op.drop_table("alert_occurrences")
    op.drop_index("ix_incidents_updated_at", table_name="incidents")
    op.drop_index("ix_incidents_status", table_name="incidents")
    op.drop_table("incidents")
