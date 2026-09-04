"""Concurrency-safe human approval decisions (Stage 08, no remediation execution)."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.config import Settings
from packages.incidents.transitions import InvalidStatusTransition, StatusTransitionService
from packages.models.incidents import IncidentStatus
from packages.models.investigation import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalResponse,
)
from packages.persistence.approval_rows import ApprovalRow
from packages.persistence.database import create_database_engine
from packages.persistence.incident_rows import AuditEventRow, IncidentRow
from packages.persistence.investigation_rows import RecommendationRow


class ApprovalStoreUnavailable(Exception):
    """PostgreSQL could not safely complete an approval operation."""


class ApprovalNotFound(Exception):
    """The referenced recommendation or incident does not exist."""


class ApprovalConflict(Exception):
    """The decision was rejected for a client-actionable reason in ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SqlAlchemyApprovalStore:
    """Record approve/reject decisions with version checks and idempotent replay."""

    def __init__(self, settings: Settings, *, engine: AsyncEngine | None = None) -> None:
        self._engine: AsyncEngine = engine or create_database_engine(settings)
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self._transitions = StatusTransitionService()

    async def decide(
        self,
        recommendation_id: str,
        *,
        incident_version: int,
        actor: str,
        decision: ApprovalDecision,
        idempotency_key: str,
    ) -> ApprovalResponse:
        """Approve or reject one waiting recommendation exactly once per key scope.

        Replay with the same idempotency key and decision returns the stored
        record with ``replayed=True``. Any conflicting reuse raises
        ``ApprovalConflict``. Approval never executes remediation: an approval
        leaves the incident in ``waiting_for_approval`` (the durable pause);
        a rejection moves it to ``rejected``.
        """

        now = datetime.now(UTC)
        try:
            async with self._sessions() as session, session.begin():
                recommendation = (
                    await session.execute(
                        select(RecommendationRow)
                        .where(RecommendationRow.id == recommendation_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if recommendation is None:
                    raise ApprovalNotFound(f"recommendation {recommendation_id} not found")
                existing = (
                    await session.execute(
                        select(ApprovalRow)
                        .where(ApprovalRow.recommendation_id == recommendation_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if (
                        existing.idempotency_key == idempotency_key
                        and existing.decision == decision.value
                    ):
                        return ApprovalResponse(approval=_to_record(existing), replayed=True)
                    raise ApprovalConflict(
                        "approval_conflict",
                        "recommendation already has a recorded decision",
                    )
                if recommendation.status != "waiting_for_approval":
                    raise ApprovalConflict(
                        "not_awaiting_approval",
                        f"recommendation is {recommendation.status}, not waiting_for_approval",
                    )
                incident = (
                    await session.execute(
                        select(IncidentRow)
                        .where(IncidentRow.id == recommendation.incident_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if incident is None:
                    raise ApprovalNotFound("incident for recommendation not found")
                if IncidentStatus(incident.status) != IncidentStatus.WAITING_FOR_APPROVAL:
                    raise ApprovalConflict(
                        "invalid_state",
                        f"incident is {incident.status}, not waiting_for_approval",
                    )
                if incident.version != incident_version:
                    raise ApprovalConflict(
                        "stale_version",
                        f"incident version {incident_version} is stale "
                        f"(current {incident.version}); refresh and retry",
                    )
                approval_id = _stable_approval_id(recommendation_id, idempotency_key)
                row = ApprovalRow(
                    id=approval_id,
                    incident_id=incident.id,
                    recommendation_id=recommendation.id,
                    run_id=recommendation.run_id,
                    report_id=recommendation.report_id,
                    decision=decision.value,
                    actor=actor,
                    incident_version=incident_version,
                    idempotency_key=idempotency_key,
                    created_at=now,
                )
                session.add(row)
                recommendation.status = decision.value
                recommendation.updated_at = now
                incident.version += 1
                incident.updated_at = now
                if decision == ApprovalDecision.REJECTED:
                    current = IncidentStatus(incident.status)
                    validated = self._transitions.transition(current, IncidentStatus.REJECTED)
                    if validated != current:
                        incident.status = validated.value
                        session.add(
                            AuditEventRow(
                                id=uuid4(),
                                incident_id=incident.id,
                                event_type="approval.rejected",
                                actor=actor,
                                from_status=current.value,
                                to_status=validated.value,
                                details={
                                    "recommendation_id": recommendation.id,
                                    "report_id": recommendation.report_id,
                                    "run_id": str(recommendation.run_id),
                                },
                            )
                        )
                session.add(
                    AuditEventRow(
                        id=uuid4(),
                        incident_id=incident.id,
                        event_type="approval.recorded",
                        actor=actor,
                        from_status=None,
                        to_status=None,
                        details={
                            "recommendation_id": recommendation.id,
                            "report_id": recommendation.report_id,
                            "run_id": str(recommendation.run_id),
                            "decision": decision.value,
                            "approval_id": approval_id,
                        },
                    )
                )
                await session.flush()
                record = _to_record(row)
        except (ApprovalNotFound, ApprovalConflict):
            raise
        except InvalidStatusTransition as error:
            raise ApprovalConflict(
                "invalid_state",
                "incident cannot move to the decided state from its current status",
            ) from error
        except IntegrityError as error:
            raise ApprovalConflict(
                "approval_conflict", "recommendation already has a recorded decision"
            ) from error
        except SQLAlchemyError as error:
            raise ApprovalStoreUnavailable("approval decision failed") from error
        return ApprovalResponse(approval=record, replayed=False)

    async def get_approval(self, recommendation_id: str) -> ApprovalRecord | None:
        """Return the recorded decision for one recommendation, if any."""

        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        select(ApprovalRow).where(
                            ApprovalRow.recommendation_id == recommendation_id
                        )
                    )
                ).scalar_one_or_none()
        except SQLAlchemyError as error:
            raise ApprovalStoreUnavailable("approval lookup failed") from error
        return _to_record(row) if row is not None else None

    async def close(self) -> None:
        """Release pooled database connections."""

        await self._engine.dispose()


def _stable_approval_id(recommendation_id: str, idempotency_key: str) -> str:
    # Hash the raw key: redact_text collapses distinct keys (e.g. "token:abc"
    # vs "token:xyz" both become "token=[REDACTED]") and must never feed identity.
    digest = hashlib.sha256(f"{recommendation_id}:{idempotency_key}".encode()).hexdigest()
    return f"APR-{digest[:24].upper()}"


def _to_record(row: ApprovalRow) -> ApprovalRecord:
    return ApprovalRecord(
        id=row.id,
        incident_id=row.incident_id,
        recommendation_id=row.recommendation_id,
        run_id=str(row.run_id),
        report_id=row.report_id,
        decision=ApprovalDecision(row.decision),
        actor=row.actor,
        incident_version=row.incident_version,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
    )
