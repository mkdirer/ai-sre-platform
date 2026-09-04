"""Durable approval-gated remediation executions with version checks and replay."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.config import Settings
from packages.incidents.transitions import InvalidStatusTransition, StatusTransitionService
from packages.models.evidence import EvidenceService
from packages.models.incidents import IncidentStatus
from packages.models.investigation import RecommendationAction
from packages.models.remediation import (
    ExecutionStatus,
    ForbiddenRemediationAction,
    RecommendationContext,
    RemediationExecution,
)
from packages.persistence.approval_rows import ApprovalRow
from packages.persistence.database import create_database_engine
from packages.persistence.incident_rows import AuditEventRow, IncidentRow
from packages.persistence.investigation_rows import RecommendationRow
from packages.persistence.remediation_rows import RemediationExecutionRow
from packages.remediation.registry import action_name_for, validate_rollback_params


class RemediationStoreUnavailable(Exception):
    """Remediation persistence is unreachable."""


class RemediationNotFound(Exception):
    """No remediation execution or backing record exists."""


class RemediationConflict(Exception):
    """Stale, mismatched, duplicate, or terminal-state execution request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _stable_execution_id(recommendation_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{recommendation_id}:{idempotency_key}".encode()).hexdigest()
    return f"REM-{digest[:24].upper()}"


def _to_execution(row: RemediationExecutionRow) -> RemediationExecution:
    return RemediationExecution(
        id=row.id,
        incident_id=row.incident_id,
        recommendation_id=row.recommendation_id,
        approval_id=row.approval_id,
        action_type=row.action_type,  # type: ignore[arg-type]
        action_name=row.action_name,  # type: ignore[arg-type]
        target=row.target,  # type: ignore[arg-type]
        incident_version=row.incident_version,
        status=ExecutionStatus(row.status),
        attempts=row.attempts,
        stop_requested=row.stop_requested,
        result=dict(row.result),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyRemediationStore:
    """Claim executions exactly once per recommendation with audited transitions."""

    def __init__(self, settings: Settings, *, engine: AsyncEngine | None = None) -> None:
        self._engine: AsyncEngine = engine or create_database_engine(settings)
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self._transitions = StatusTransitionService()

    async def request_execution(
        self,
        recommendation_id: str,
        *,
        incident_version: int,
        expected_service_version: str,
        actor: str,
        idempotency_key: str,
    ) -> tuple[RemediationExecution, bool]:
        """Claim one execution row and move the incident to remediating.

        Same key replays the stored execution. A terminal completed row
        rejects with already_completed; failed/stopped rows are reclaimed
        for one new attempt under a fresh key. Anything else active
        conflicts as execution_in_progress.
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
                    raise RemediationNotFound(f"recommendation {recommendation_id} not found")
                if recommendation.status != "approved":
                    raise RemediationConflict(
                        "not_approved",
                        f"recommendation is {recommendation.status}, not approved",
                    )
                try:
                    action_name = action_name_for(RecommendationAction(recommendation.action_type))
                    validate_rollback_params(
                        action=RecommendationAction(recommendation.action_type),
                        target=EvidenceService(recommendation.target),
                        parameters=dict(recommendation.parameters),
                    )
                except (ForbiddenRemediationAction, ValueError) as error:
                    code = (
                        error.code
                        if isinstance(error, ForbiddenRemediationAction)
                        else "forbidden_action"
                    )
                    raise RemediationConflict(code, str(error)) from error
                approval = (
                    await session.execute(
                        select(ApprovalRow)
                        .where(ApprovalRow.recommendation_id == recommendation_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if approval is None or approval.decision != "approved":
                    raise RemediationConflict(
                        "not_approved", "recommendation has no recorded approval"
                    )
                incident = (
                    await session.execute(
                        select(IncidentRow)
                        .where(IncidentRow.id == recommendation.incident_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if incident is None:
                    raise RemediationNotFound("incident for recommendation not found")
                existing = (
                    await session.execute(
                        select(RemediationExecutionRow)
                        .where(RemediationExecutionRow.recommendation_id == recommendation_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.idempotency_key == idempotency_key:
                        return _to_execution(existing), True
                    if existing.status == ExecutionStatus.COMPLETED.value:
                        raise RemediationConflict(
                            "already_completed",
                            "remediation already completed; replays return the record",
                        )
                    if existing.status == ExecutionStatus.PENDING.value:
                        raise RemediationConflict(
                            "execution_in_progress",
                            "remediation is pending; concurrent execution refused",
                        )
                if IncidentStatus(incident.status) != IncidentStatus.WAITING_FOR_APPROVAL:
                    raise RemediationConflict(
                        "invalid_state",
                        f"incident is {incident.status}, not waiting_for_approval",
                    )
                if incident.version != incident_version:
                    raise RemediationConflict(
                        "stale_version",
                        f"incident version {incident_version} is stale "
                        f"(current {incident.version}); refresh and retry",
                    )
                if existing is not None:
                    # Reclaim after the incident returned to waiting through a
                    # new investigation cycle: a superseded live loop fails
                    # safely on its next guarded mark (status mismatch), so
                    # resetting here cannot double-execute.
                    existing.status = ExecutionStatus.PENDING.value
                    existing.attempts = 0
                    existing.stop_requested = False
                    existing.idempotency_key = idempotency_key
                    existing.incident_version = incident_version
                    existing.result = {"expected_service_version": expected_service_version}
                    existing.updated_at = now
                    self._transition(
                        session,
                        incident,
                        IncidentStatus.REMEDIATING,
                        actor=actor,
                        event_type="remediation.reclaimed",
                        now=now,
                        details={
                            "execution_id": existing.id,
                            "recommendation_id": recommendation.id,
                            "idempotency_key": idempotency_key,
                        },
                    )
                    await session.flush()
                    return _to_execution(existing), False
                execution_id = _stable_execution_id(recommendation_id, idempotency_key)
                row = RemediationExecutionRow(
                    id=execution_id,
                    incident_id=incident.id,
                    recommendation_id=recommendation.id,
                    approval_id=approval.id,
                    action_type=recommendation.action_type,
                    action_name=action_name,
                    target=recommendation.target,
                    incident_version=incident_version,
                    idempotency_key=idempotency_key,
                    status=ExecutionStatus.PENDING.value,
                    attempts=0,
                    stop_requested=False,
                    result={"expected_service_version": expected_service_version},
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                self._transition(
                    session,
                    incident,
                    IncidentStatus.REMEDIATING,
                    actor=actor,
                    event_type="remediation.claimed",
                    now=now,
                    details={
                        "execution_id": execution_id,
                        "recommendation_id": recommendation.id,
                        "approval_id": approval.id,
                        "expected_service_version": expected_service_version,
                    },
                )
                await session.flush()
                return _to_execution(row), False
        except (RemediationNotFound, RemediationConflict):
            raise
        except InvalidStatusTransition as error:
            raise RemediationConflict(
                "invalid_state", "incident cannot enter remediation from its state"
            ) from error
        except IntegrityError as error:
            raise RemediationConflict(
                "execution_in_progress", "concurrent execution claim lost the race"
            ) from error
        except SQLAlchemyError as error:
            raise RemediationStoreUnavailable("remediation claim failed") from error

    async def get_execution(self, execution_id: str) -> RemediationExecution | None:
        """Return one execution record, if present."""

        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        select(RemediationExecutionRow).where(
                            RemediationExecutionRow.id == execution_id
                        )
                    )
                ).scalar_one_or_none()
                return _to_execution(row) if row is not None else None
        except SQLAlchemyError as error:
            raise RemediationStoreUnavailable("remediation read failed") from error

    async def recommendation_for(self, execution_id: str) -> RecommendationContext:
        """Return the approved recommendation inputs backing one execution."""

        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        select(RecommendationRow)
                        .join(
                            RemediationExecutionRow,
                            RemediationExecutionRow.recommendation_id == RecommendationRow.id,
                        )
                        .where(RemediationExecutionRow.id == execution_id)
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise RemediationNotFound(f"execution {execution_id} not found")
                return RecommendationContext(
                    action_type=RecommendationAction(row.action_type),
                    target=EvidenceService(row.target),
                    parameters=dict(row.parameters),
                )
        except RemediationNotFound:
            raise
        except SQLAlchemyError as error:
            raise RemediationStoreUnavailable("remediation context read failed") from error

    async def mark_executing(self, execution_id: str, *, actor: str) -> RemediationExecution:
        """Move a pending execution to executing; concurrent claims lose."""

        return await self._move(
            execution_id,
            actor=actor,
            event_type="remediation.executing",
            from_statuses=(ExecutionStatus.PENDING,),
            to_execution=ExecutionStatus.EXECUTING,
            to_incident=None,
        )

    async def mark_verifying(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution:
        """Record a successful adapter run and enter verification."""

        return await self._move(
            execution_id,
            actor=actor,
            event_type="remediation.executed",
            from_statuses=(ExecutionStatus.EXECUTING,),
            to_execution=ExecutionStatus.VERIFYING,
            to_incident=IncidentStatus.VERIFYING,
            details=details,
            bump_attempts=True,
        )

    async def mark_completed(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution:
        """Record verified recovery and resolve the incident."""

        return await self._move(
            execution_id,
            actor=actor,
            event_type="remediation.verified",
            from_statuses=(ExecutionStatus.VERIFYING,),
            to_execution=ExecutionStatus.COMPLETED,
            to_incident=IncidentStatus.RESOLVED,
            details=details,
        )

    async def mark_ambiguous(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution:
        """Record inconclusive verification; the incident stays verifying."""

        return await self._move(
            execution_id,
            actor=actor,
            event_type="remediation.ambiguous",
            from_statuses=(ExecutionStatus.VERIFYING,),
            to_execution=ExecutionStatus.VERIFYING,
            to_incident=None,
            details=details,
        )

    async def mark_failed(
        self, execution_id: str, *, actor: str, details: dict[str, object]
    ) -> RemediationExecution:
        """Record execution failure; the incident is failed, never resolved."""

        return await self._move(
            execution_id,
            actor=actor,
            event_type="remediation.failed",
            from_statuses=(
                ExecutionStatus.PENDING,
                ExecutionStatus.EXECUTING,
                ExecutionStatus.VERIFYING,
            ),
            to_execution=ExecutionStatus.FAILED,
            to_incident=IncidentStatus.INVESTIGATION_FAILED,
            details=details,
            bump_attempts=True,
        )

    async def request_stop(
        self,
        execution_id: str,
        *,
        incident_version: int,
        actor: str,
    ) -> RemediationExecution:
        """Stop a live execution; repeat flags are idempotent no-ops.

        The stop is synchronous and terminal: a live loop observes the flag
        for an early graceful exit, and any later guarded mark from that
        loop conflict-terminates safely instead of corrupting state. A racing
        worker that has not yet claimed fails safely on its guarded claim.
        """

        now = datetime.now(UTC)
        try:
            async with self._sessions() as session, session.begin():
                row = await self._locked_execution(session, execution_id)
                if ExecutionStatus(row.status) in (
                    ExecutionStatus.COMPLETED,
                    ExecutionStatus.FAILED,
                    ExecutionStatus.STOPPED,
                ):
                    raise RemediationConflict(
                        "already_completed", f"execution is {row.status}; nothing to stop"
                    )
                incident = await self._locked_incident(session, row.incident_id)
                if incident.version != incident_version:
                    raise RemediationConflict(
                        "stale_version",
                        f"incident version {incident_version} is stale "
                        f"(current {incident.version})",
                    )
                if row.stop_requested and ExecutionStatus(row.status) == ExecutionStatus.STOPPED:
                    return _to_execution(row)
                row.stop_requested = True
                row.status = ExecutionStatus.STOPPED.value
                row.updated_at = now
                self._transition(
                    session,
                    incident,
                    IncidentStatus.INVESTIGATION_FAILED,
                    actor=actor,
                    event_type="remediation.stopped",
                    now=now,
                    details={"execution_id": row.id},
                )
                await session.flush()
                return _to_execution(row)
        except (RemediationNotFound, RemediationConflict):
            raise
        except InvalidStatusTransition as error:
            raise RemediationConflict(
                "invalid_state", "incident cannot leave its remediation state"
            ) from error
        except SQLAlchemyError as error:
            raise RemediationStoreUnavailable("remediation stop failed") from error

    async def _move(
        self,
        execution_id: str,
        *,
        actor: str,
        event_type: str,
        from_statuses: tuple[ExecutionStatus, ...],
        to_execution: ExecutionStatus,
        to_incident: IncidentStatus | None,
        details: dict[str, object] | None = None,
        bump_attempts: bool = False,
    ) -> RemediationExecution:
        now = datetime.now(UTC)
        try:
            async with self._sessions() as session, session.begin():
                row = await self._locked_execution(session, execution_id)
                if ExecutionStatus(row.status) not in from_statuses:
                    raise RemediationConflict(
                        "invalid_state",
                        f"execution is {row.status}; cannot record {event_type}",
                    )
                incident = await self._locked_incident(session, row.incident_id)
                if to_incident is not None:
                    self._transition(
                        session,
                        incident,
                        to_incident,
                        actor=actor,
                        event_type=event_type,
                        now=now,
                        details={"execution_id": row.id, **(details or {})},
                    )
                else:
                    self._add_audit(
                        session,
                        incident_id=row.incident_id,
                        event_type=event_type,
                        actor=actor,
                        from_status=None,
                        to_status=None,
                        details={"execution_id": row.id, **(details or {})},
                    )
                row.status = to_execution.value
                if bump_attempts:
                    row.attempts += 1
                row.result = {**(row.result or {}), **(details or {})}
                row.updated_at = now
                await session.flush()
                return _to_execution(row)
        except (RemediationNotFound, RemediationConflict):
            raise
        except InvalidStatusTransition as error:
            raise RemediationConflict(
                "invalid_state", "incident cannot take the remediation transition"
            ) from error
        except SQLAlchemyError as error:
            raise RemediationStoreUnavailable("remediation transition failed") from error

    async def _locked_execution(
        self, session: AsyncSession, execution_id: str
    ) -> RemediationExecutionRow:
        row = (
            await session.execute(
                select(RemediationExecutionRow)
                .where(RemediationExecutionRow.id == execution_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise RemediationNotFound(f"execution {execution_id} not found")
        return row

    async def _locked_incident(self, session: AsyncSession, incident_id: str) -> IncidentRow:
        incident = (
            await session.execute(
                select(IncidentRow).where(IncidentRow.id == incident_id).with_for_update()
            )
        ).scalar_one_or_none()
        if incident is None:
            raise RemediationNotFound("incident for execution not found")
        return incident

    def _transition(
        self,
        session: AsyncSession,
        incident: IncidentRow,
        target: IncidentStatus,
        *,
        actor: str,
        event_type: str,
        now: datetime,
        details: dict[str, object] | None = None,
    ) -> None:
        current = IncidentStatus(incident.status)
        validated = self._transitions.transition(current, target)
        if validated == current:
            return
        incident.status = validated.value
        incident.version += 1
        incident.updated_at = now
        session.add(
            AuditEventRow(
                id=uuid4(),
                incident_id=incident.id,
                event_type=event_type,
                actor=actor,
                from_status=current.value,
                to_status=validated.value,
                details=details or {},
            )
        )

    @staticmethod
    def _add_audit(
        session: AsyncSession,
        *,
        incident_id: str,
        event_type: str,
        actor: str,
        from_status: IncidentStatus | None,
        to_status: IncidentStatus | None,
        details: dict[str, object],
    ) -> None:
        session.add(
            AuditEventRow(
                id=uuid4(),
                incident_id=incident_id,
                event_type=event_type,
                actor=actor,
                from_status=from_status.value if from_status is not None else None,
                to_status=to_status.value if to_status is not None else None,
                details=details,
            )
        )

    async def close(self) -> None:
        """Dispose the owned engine."""

        await self._engine.dispose()
