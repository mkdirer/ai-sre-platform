"""SQLAlchemy mappings for durable Stage 04 incident and queue state."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    CHAR,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence.database import Base


class IncidentRow(Base):
    """Canonical current state for one deduplicated alert identity."""

    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("alert_fingerprint", name="uq_incidents_alert_fingerprint"),
        CheckConstraint(
            "status IN ('queued','investigating','waiting_for_approval','remediating',"
            "'verifying','resolved','insufficient_evidence','investigation_failed',"
            "'rejected','closed')",
            name="valid_status",
        ),
        CheckConstraint(
            "severity IN ('info','warning','critical')",
            name="valid_severity",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="valid_confidence",
        ),
        CheckConstraint("version >= 1", name="positive_version"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    alert_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    alert_name: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    affected_services: Mapped[list[str]] = mapped_column(ARRAY(String(128)), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_alert_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    investigation_window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    investigation_window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    root_cause: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        index=True,
    )


class AlertOccurrenceRow(Base):
    """Immutable normalized Alertmanager state update."""

    __tablename__ = "alert_occurrences"
    __table_args__ = (
        UniqueConstraint("delivery_fingerprint", name="uq_alert_occurrences_delivery_fingerprint"),
        CheckConstraint("status IN ('firing','resolved')", name="valid_status"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_fingerprint: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    labels: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    annotations: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class InvestigationRunRow(Base):
    """One retry-aware no-AI placeholder investigation run."""

    __tablename__ = "investigation_runs"
    __table_args__ = (
        CheckConstraint("stage = 'no_ai_placeholder'", name="valid_stage"),
        CheckConstraint(
            "status IN ('queued','running','placeholder_complete_no_ai','retry_scheduled',"
            "'failed','dead_lettered','skipped_terminal')",
            name="valid_status",
        ),
        CheckConstraint("attempt >= 0", name="nonnegative_attempt"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(String(512))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class QueueJobRow(Base):
    """Outbox, retry, lease, and dead-letter state for an incident task."""

    __tablename__ = "queue_jobs"
    __table_args__ = (
        UniqueConstraint("celery_task_id", name="uq_queue_jobs_celery_task_id"),
        UniqueConstraint("investigation_run_id", name="uq_queue_jobs_investigation_run_id"),
        CheckConstraint(
            "status IN ('pending_publish','queued','processing','retry_scheduled','completed',"
            "'publish_failed','dead_lettered','skipped_terminal')",
            name="valid_status",
        ),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint("max_attempts >= 1", name="positive_max_attempts"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    investigation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False
    )
    celery_task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error_type: Mapped[str | None] = mapped_column(String(128))
    last_error_message: Mapped[str | None] = mapped_column(String(512))
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class AuditEventRow(Base):
    """Immutable audit entry for ingestion, transitions, queue, and worker actions."""

    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
