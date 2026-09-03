"""SQLAlchemy mappings for Stage 3 evidence and local deployment metadata."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence.database import Base


class EvidenceRow(Base):
    """Canonical incident-owned result for one allowlisted query template."""

    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(
            "source IN ('prometheus','loki','tempo','deployment_store')",
            name="valid_source",
        ),
        CheckConstraint(
            "evidence_type IN ('metric','log','trace','deployment')",
            name="valid_type",
        ),
        CheckConstraint(
            "status IN ('collected','empty','unavailable','failed','timed_out')",
            name="valid_status",
        ),
        CheckConstraint("window_end >= window_start", name="valid_window"),
        CheckConstraint(
            "((status IN ('unavailable','failed','timed_out')) AND error_type IS NOT NULL "
            "AND error_message IS NOT NULL) OR "
            "((status IN ('collected','empty')) AND error_type IS NULL "
            "AND error_message IS NULL)",
            name="valid_error_state",
        ),
        Index("ix_evidence_incident_observed", "incident_id", "observed_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(28), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    evidence_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[str] = mapped_column(String(512), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    query_template: Mapped[str] = mapped_column(String(64), nullable=False)
    query_parameters: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    provenance: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(String(512))
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class DeploymentRow(Base):
    """Immutable local deployment fact, independent of any remote source-code host."""

    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint(
            "service",
            "environment",
            "version",
            "deployed_at",
            "commit_sha",
            name="uq_deployments_identity",
        ),
        Index(
            "ix_deployments_service_environment_time",
            "service",
            "environment",
            "deployed_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_files: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    deployment_metadata: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
