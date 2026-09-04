"""SQLAlchemy mappings for durable Stage 06 investigator artifacts."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence.database import Base


class HypothesisRow(Base):
    """One stable hypothesis for an investigation run."""

    __tablename__ = "hypotheses"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed','verified','rejected','inconclusive')",
            name="valid_status",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="valid_confidence"),
    )

    id: Mapped[str] = mapped_column(String(28), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(ARRAY(String(28)), nullable=False)
    contradicting_evidence_ids: Mapped[list[str]] = mapped_column(ARRAY(String(28)), nullable=False)
    reasoning_summary: Mapped[str] = mapped_column(String(512), nullable=False)
    next_evidence_requests: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IncidentReportRow(Base):
    """One canonical structured report per retry-safe run."""

    __tablename__ = "incident_reports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('complete','insufficient_evidence','waiting_for_approval')",
            name="valid_status",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="valid_confidence"),
    )

    id: Mapped[str] = mapped_column(String(28), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    root_cause: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    report: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecommendationRow(Base):
    """A persisted proposal with no execution fields or execution adapter."""

    __tablename__ = "recommendations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed','waiting_for_approval','approved','rejected')",
            name="valid_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(28), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[str] = mapped_column(
        ForeignKey("incident_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(128), nullable=False)
    parameters: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    rationale_evidence_ids: Mapped[list[str]] = mapped_column(ARRAY(String(28)), nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    reversible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvestigatorCallRow(Base):
    """Bounded, secret-free provider/tool attempt metadata."""

    __tablename__ = "investigator_calls"
    __table_args__ = (
        CheckConstraint("kind IN ('model','tool')", name="valid_kind"),
        CheckConstraint("status IN ('succeeded','failed','rejected')", name="valid_status"),
        CheckConstraint("attempt >= 1", name="positive_attempt"),
        CheckConstraint("duration_seconds >= 0", name="nonnegative_duration"),
        CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="nonnegative_cost",
        ),
    )

    id: Mapped[str] = mapped_column(String(29), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(String(512))
    call_metadata: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvestigationFailureRow(Base):
    """Idempotent workflow-level failure audit independent of queue retries."""

    __tablename__ = "investigation_failures"

    id: Mapped[str] = mapped_column(String(29), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    error_message: Mapped[str] = mapped_column(String(512), nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
