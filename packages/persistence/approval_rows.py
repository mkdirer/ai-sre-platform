"""SQLAlchemy mapping for durable human approval decisions (Stage 08)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence.database import Base


class ApprovalRow(Base):
    """One immutable human decision per recommendation with idempotency provenance."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("recommendation_id", name="uq_approvals_recommendation_id"),
        CheckConstraint("decision IN ('approved','rejected')", name="valid_decision"),
    )

    # Stable content-derived ID (APR-...): the exposed API ID is the persisted
    # primary key, so audit events can join on it without recomputation.
    id: Mapped[str] = mapped_column(String(28), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("recommendations.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[str] = mapped_column(
        ForeignKey("incident_reports.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_version: Mapped[int] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
