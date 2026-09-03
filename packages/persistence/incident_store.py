"""PostgreSQL incident repository with atomic deduplication and durable queue tracking."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.config import Settings
from packages.incidents import NormalizedAlert, StatusTransitionService
from packages.models.incidents import (
    AlertAcceptance,
    AuditEventPage,
    AuditEventResponse,
    IncidentDetail,
    IncidentPage,
    IncidentSeverity,
    IncidentStatus,
    IncidentSummary,
    InvestigationRunPage,
    InvestigationRunResponse,
    InvestigationRunStatus,
    QueueJobPage,
    QueueJobResponse,
    QueueJobStatus,
)
from packages.persistence.database import create_database_engine
from packages.persistence.incident_rows import (
    AlertOccurrenceRow,
    AuditEventRow,
    IncidentRow,
    InvestigationRunRow,
    QueueJobRow,
)
from packages.telemetry import TelemetryRuntime, redact_text

_OCCURRENCE_NAMESPACE = uuid5(NAMESPACE_URL, "ai-sre-platform/alert-occurrence")
_RUN_NAMESPACE = uuid5(NAMESPACE_URL, "ai-sre-platform/investigation-run")
_JOB_NAMESPACE = uuid5(NAMESPACE_URL, "ai-sre-platform/queue-job")
_TERMINAL_JOB_STATUSES = frozenset(
    {
        QueueJobStatus.COMPLETED,
        QueueJobStatus.DEAD_LETTERED,
        QueueJobStatus.SKIPPED_TERMINAL,
    }
)
_INCIDENTS_WITHOUT_WORK = frozenset(
    {IncidentStatus.RESOLVED, IncidentStatus.REJECTED, IncidentStatus.CLOSED}
)
_REOPENABLE_STATUSES = frozenset(
    {
        IncidentStatus.RESOLVED,
        IncidentStatus.INSUFFICIENT_EVIDENCE,
        IncidentStatus.INVESTIGATION_FAILED,
        IncidentStatus.REJECTED,
        IncidentStatus.CLOSED,
    }
)


class IncidentStoreUnavailable(Exception):
    """PostgreSQL could not safely complete an incident operation."""


class QueueJobNotFound(Exception):
    """The worker task ID does not identify its declared incident job."""


@dataclass(frozen=True)
class PendingQueueJob:
    """A committed outbox row that is safe to publish to Celery."""

    id: UUID
    incident_id: str


@dataclass(frozen=True)
class IngestBatch:
    """Committed ingestion results and jobs requiring post-commit publication."""

    acceptances: tuple[AlertAcceptance, ...]
    pending_jobs: tuple[PendingQueueJob, ...]


@dataclass(frozen=True)
class WorkerClaim:
    """Canonical incident and retry metadata loaded while claiming a durable job."""

    claimed: bool
    reason: str
    job_id: UUID
    run_id: UUID
    incident_id: str
    incident_title: str
    service: str
    affected_services: tuple[str, ...]
    started_at: datetime
    investigation_window_start: datetime
    investigation_window_end: datetime
    attempt: int
    max_attempts: int


class SqlAlchemyIncidentStore:
    """Concurrency-safe store for ingestion, API reads, and worker transitions."""

    def __init__(
        self,
        settings: Settings,
        *,
        engine: AsyncEngine | None = None,
        telemetry: TelemetryRuntime | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine or create_database_engine(settings)
        if telemetry is not None:
            telemetry.instrument_sqlalchemy_engine(self._engine)
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self._transitions = StatusTransitionService()

    async def ingest(self, alerts: tuple[NormalizedAlert, ...]) -> IngestBatch:
        """Persist a webhook atomically and return committed jobs needing publication."""

        if not alerts:
            raise ValueError("At least one normalized alert is required")
        acceptances: list[AlertAcceptance] = []
        pending_jobs: dict[UUID, PendingQueueJob] = {}
        try:
            async with self._sessions() as session, session.begin():
                for alert in sorted(
                    alerts,
                    key=lambda item: (item.alert_fingerprint, item.delivery_fingerprint),
                ):
                    acceptance, jobs = await self._ingest_one(session, alert)
                    acceptances.append(acceptance)
                    for job in jobs:
                        pending_jobs[job.id] = job
        except SQLAlchemyError as error:
            raise IncidentStoreUnavailable("alert ingestion failed") from error
        return IngestBatch(tuple(acceptances), tuple(pending_jobs.values()))

    async def _ingest_one(
        self,
        session: AsyncSession,
        alert: NormalizedAlert,
    ) -> tuple[AlertAcceptance, tuple[PendingQueueJob, ...]]:
        now = datetime.now(UTC)
        incident_id = _public_incident_id(alert.alert_fingerprint)
        initial_status = (
            IncidentStatus.QUEUED if alert.status == "firing" else IncidentStatus.RESOLVED
        )
        created_id = (
            await session.execute(
                insert(IncidentRow)
                .values(
                    id=incident_id,
                    alert_fingerprint=alert.alert_fingerprint,
                    alert_name=alert.alert_name,
                    title=alert.title,
                    service=alert.service,
                    affected_services=list(alert.affected_services),
                    severity=alert.severity.value,
                    status=initial_status.value,
                    started_at=alert.starts_at,
                    last_alert_at=now,
                    resolved_at=alert.ends_at if alert.status == "resolved" else None,
                    completed_at=alert.ends_at if alert.status == "resolved" else None,
                    investigation_window_start=alert.investigation_window_start,
                    investigation_window_end=alert.investigation_window_end,
                    root_cause=None,
                    confidence=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=[IncidentRow.alert_fingerprint])
                .returning(IncidentRow.id)
            )
        ).scalar_one_or_none()
        incident = (
            await session.execute(
                select(IncidentRow)
                .where(IncidentRow.alert_fingerprint == alert.alert_fingerprint)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if incident is None:
            raise IncidentStoreUnavailable("incident identity collision")
        created = created_id is not None

        occurrence_id = uuid5(_OCCURRENCE_NAMESPACE, alert.delivery_fingerprint)
        recorded_id = (
            await session.execute(
                insert(AlertOccurrenceRow)
                .values(
                    id=occurrence_id,
                    incident_id=incident.id,
                    delivery_fingerprint=alert.delivery_fingerprint,
                    source_fingerprint=alert.source_fingerprint,
                    status=alert.status,
                    labels=alert.labels,
                    annotations=alert.annotations,
                    starts_at=alert.starts_at,
                    ends_at=alert.ends_at,
                    received_at=now,
                )
                .on_conflict_do_nothing(index_elements=[AlertOccurrenceRow.delivery_fingerprint])
                .returning(AlertOccurrenceRow.id)
            )
        ).scalar_one_or_none()
        occurrence_recorded = recorded_id is not None

        if created:
            self._add_audit(
                session,
                incident_id=incident.id,
                event_type="incident.created",
                actor="incident-api",
                from_status=None,
                to_status=initial_status,
                details={"alert_name": alert.alert_name, "severity": alert.severity.value},
            )

        jobs: tuple[PendingQueueJob, ...] = ()
        if occurrence_recorded:
            self._add_audit(
                session,
                incident_id=incident.id,
                event_type="alert.occurrence_recorded",
                actor="alertmanager",
                from_status=None,
                to_status=None,
                details={
                    "alert_status": alert.status,
                    "delivery_fingerprint": alert.delivery_fingerprint,
                },
            )
            if not created:
                incident.alert_name = alert.alert_name
                incident.title = alert.title
                incident.service = alert.service
                incident.affected_services = list(alert.affected_services)
                incident.severity = alert.severity.value
                incident.last_alert_at = now
                incident.investigation_window_end = max(
                    incident.investigation_window_end,
                    alert.investigation_window_end,
                )
                incident.version += 1
                incident.updated_at = now

            if alert.status == "firing":
                current_status = IncidentStatus(incident.status)
                should_start_run = created or current_status in _REOPENABLE_STATUSES
                if not created and current_status in _REOPENABLE_STATUSES:
                    self._transition(
                        session,
                        incident,
                        IncidentStatus.QUEUED,
                        actor="incident-api",
                        event_type="incident.reopened",
                        now=now,
                    )
                    incident.started_at = alert.starts_at
                    incident.investigation_window_start = alert.investigation_window_start
                    incident.investigation_window_end = alert.investigation_window_end
                    incident.resolved_at = None
                    incident.completed_at = None
                    incident.root_cause = None
                    incident.confidence = None
                if should_start_run:
                    jobs = (
                        self._create_investigation_job(
                            session,
                            incident_id=incident.id,
                            delivery_fingerprint=alert.delivery_fingerprint,
                            now=now,
                        ),
                    )
            else:
                current_status = IncidentStatus(incident.status)
                if not created and current_status != IncidentStatus.RESOLVED:
                    self._transition(
                        session,
                        incident,
                        IncidentStatus.RESOLVED,
                        actor="alertmanager",
                        event_type="incident.resolved_by_alert",
                        now=now,
                    )
                incident.resolved_at = alert.ends_at or now
                incident.completed_at = alert.ends_at or now
        else:
            jobs = await self._pending_publish_jobs(session, incident.id)

        return (
            AlertAcceptance(
                incident_id=incident.id,
                alert_fingerprint=alert.alert_fingerprint,
                incident_status=IncidentStatus(incident.status),
                occurrence_recorded=occurrence_recorded,
                duplicate=not occurrence_recorded,
                investigation_enqueued=False,
            ),
            jobs,
        )

    def _create_investigation_job(
        self,
        session: AsyncSession,
        *,
        incident_id: str,
        delivery_fingerprint: str,
        now: datetime,
    ) -> PendingQueueJob:
        run_id = uuid5(_RUN_NAMESPACE, f"{incident_id}:{delivery_fingerprint}")
        job_id = uuid5(_JOB_NAMESPACE, str(run_id))
        session.add(
            InvestigationRunRow(
                id=run_id,
                incident_id=incident_id,
                stage="evidence_collection",
                status=InvestigationRunStatus.QUEUED.value,
                attempt=0,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            QueueJobRow(
                id=job_id,
                incident_id=incident_id,
                investigation_run_id=run_id,
                celery_task_id=str(job_id),
                status=QueueJobStatus.PENDING_PUBLISH.value,
                attempts=0,
                max_attempts=self._settings.investigation_max_attempts,
                created_at=now,
                updated_at=now,
            )
        )
        self._add_audit(
            session,
            incident_id=incident_id,
            event_type="investigation.evidence_collection_queued",
            actor="incident-api",
            from_status=None,
            to_status=None,
            details={"run_id": str(run_id), "job_id": str(job_id), "stage": "evidence_collection"},
        )
        return PendingQueueJob(id=job_id, incident_id=incident_id)

    async def _pending_publish_jobs(
        self,
        session: AsyncSession,
        incident_id: str,
    ) -> tuple[PendingQueueJob, ...]:
        rows = (
            await session.execute(
                select(QueueJobRow).where(
                    QueueJobRow.incident_id == incident_id,
                    QueueJobRow.status.in_(
                        [
                            QueueJobStatus.PENDING_PUBLISH.value,
                            QueueJobStatus.PUBLISH_FAILED.value,
                        ]
                    ),
                )
            )
        ).scalars()
        return tuple(PendingQueueJob(id=row.id, incident_id=row.incident_id) for row in rows)

    async def mark_job_published(self, job_id: UUID) -> None:
        """Move a committed outbox row to queued without clobbering a fast worker."""

        now = datetime.now(UTC)
        try:
            async with self._sessions() as session, session.begin():
                job = (
                    await session.execute(
                        select(QueueJobRow).where(QueueJobRow.id == job_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if job is None:
                    raise QueueJobNotFound(str(job_id))
                current_status = QueueJobStatus(job.status)
                if current_status in {
                    QueueJobStatus.PENDING_PUBLISH,
                    QueueJobStatus.PUBLISH_FAILED,
                }:
                    job.status = QueueJobStatus.QUEUED.value
                if job.enqueued_at is None:
                    job.enqueued_at = now
                    job.last_error_type = None
                    job.last_error_message = None
                    job.updated_at = now
                    self._add_audit(
                        session,
                        incident_id=job.incident_id,
                        event_type="queue.job_published",
                        actor="incident-api",
                        from_status=None,
                        to_status=None,
                        details={"job_id": str(job.id)},
                    )
        except QueueJobNotFound:
            raise
        except SQLAlchemyError as error:
            raise IncidentStoreUnavailable("could not record queue publication") from error

    async def mark_job_publish_failed(self, job_id: UUID, error: Exception) -> None:
        """Expose broker publication failure so a duplicate webhook can retry it."""

        now = datetime.now(UTC)
        try:
            async with self._sessions() as session, session.begin():
                job = (
                    await session.execute(
                        select(QueueJobRow).where(QueueJobRow.id == job_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if job is None:
                    raise QueueJobNotFound(str(job_id))
                if QueueJobStatus(job.status) not in _TERMINAL_JOB_STATUSES:
                    job.status = QueueJobStatus.PUBLISH_FAILED.value
                    job.last_error_type = type(error).__name__[:128]
                    job.last_error_message = redact_text(str(error))[:512]
                    job.updated_at = now
                    self._add_audit(
                        session,
                        incident_id=job.incident_id,
                        event_type="queue.publish_failed",
                        actor="incident-api",
                        from_status=None,
                        to_status=None,
                        details={
                            "job_id": str(job.id),
                            "error_type": type(error).__name__[:128],
                        },
                    )
        except QueueJobNotFound:
            raise
        except SQLAlchemyError as store_error:
            raise IncidentStoreUnavailable(
                "could not record queue publication failure"
            ) from store_error

    async def claim_job(self, job_id: UUID, incident_id: str) -> WorkerClaim:
        """Atomically load canonical state and acquire/reacquire a bounded worker lease."""

        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=self._settings.investigation_job_lease_seconds)
        try:
            async with self._sessions() as session, session.begin():
                job = (
                    await session.execute(
                        select(QueueJobRow).where(QueueJobRow.id == job_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if job is None or job.incident_id != incident_id:
                    raise QueueJobNotFound(str(job_id))
                incident = (
                    await session.execute(
                        select(IncidentRow).where(IncidentRow.id == incident_id).with_for_update()
                    )
                ).scalar_one()
                run = (
                    await session.execute(
                        select(InvestigationRunRow)
                        .where(InvestigationRunRow.id == job.investigation_run_id)
                        .with_for_update()
                    )
                ).scalar_one()
                current_job_status = QueueJobStatus(job.status)
                if current_job_status in _TERMINAL_JOB_STATUSES:
                    return self._worker_claim(job, incident, claimed=False, reason="terminal")
                if (
                    current_job_status == QueueJobStatus.PROCESSING
                    and job.lease_expires_at is not None
                    and job.lease_expires_at > now
                ):
                    return self._worker_claim(job, incident, claimed=False, reason="lease_active")
                incident_status = IncidentStatus(incident.status)
                if incident_status in _INCIDENTS_WITHOUT_WORK:
                    job.status = QueueJobStatus.SKIPPED_TERMINAL.value
                    job.finished_at = now
                    job.lease_expires_at = None
                    job.updated_at = now
                    run.status = InvestigationRunStatus.SKIPPED_TERMINAL.value
                    run.completed_at = now
                    run.updated_at = now
                    self._add_audit(
                        session,
                        incident_id=incident.id,
                        event_type="investigation.evidence_collection_skipped_terminal",
                        actor="investigator-worker",
                        from_status=None,
                        to_status=None,
                        details={"job_id": str(job.id), "incident_status": incident.status},
                    )
                    return self._worker_claim(
                        job, incident, claimed=False, reason="incident_terminal"
                    )
                if incident_status == IncidentStatus.QUEUED:
                    self._transition(
                        session,
                        incident,
                        IncidentStatus.INVESTIGATING,
                        actor="investigator-worker",
                        event_type="investigation.evidence_collection_started",
                        now=now,
                    )
                elif incident_status != IncidentStatus.INVESTIGATING:
                    return self._worker_claim(
                        job, incident, claimed=False, reason="incident_not_claimable"
                    )
                job.status = QueueJobStatus.PROCESSING.value
                job.attempts += 1
                job.started_at = now
                job.next_retry_at = None
                job.lease_expires_at = lease_expires_at
                job.updated_at = now
                run.status = InvestigationRunStatus.RUNNING.value
                run.stage = "evidence_collection"
                run.attempt = job.attempts
                run.error_type = None
                run.error_message = None
                run.started_at = run.started_at or now
                run.updated_at = now
                self._add_audit(
                    session,
                    incident_id=incident.id,
                    event_type="queue.job_claimed",
                    actor="investigator-worker",
                    from_status=None,
                    to_status=None,
                    details={"job_id": str(job.id), "attempt": job.attempts},
                )
                return self._worker_claim(job, incident, claimed=True, reason="claimed")
        except QueueJobNotFound:
            raise
        except SQLAlchemyError as error:
            raise IncidentStoreUnavailable("could not claim investigation job") from error

    async def complete_evidence_job(
        self,
        job_id: UUID,
        *,
        source_summaries: list[dict[str, object]],
    ) -> None:
        """Idempotently mark deterministic evidence collection complete."""

        now = datetime.now(UTC)
        try:
            async with self._sessions() as session, session.begin():
                job = (
                    await session.execute(
                        select(QueueJobRow).where(QueueJobRow.id == job_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if job is None:
                    raise QueueJobNotFound(str(job_id))
                if QueueJobStatus(job.status) in _TERMINAL_JOB_STATUSES:
                    return
                run = (
                    await session.execute(
                        select(InvestigationRunRow)
                        .where(InvestigationRunRow.id == job.investigation_run_id)
                        .with_for_update()
                    )
                ).scalar_one()
                job.status = QueueJobStatus.COMPLETED.value
                job.finished_at = now
                job.lease_expires_at = None
                job.next_retry_at = None
                job.updated_at = now
                run.stage = "evidence_collection"
                run.status = InvestigationRunStatus.EVIDENCE_COLLECTED.value
                run.completed_at = now
                run.updated_at = now
                self._add_audit(
                    session,
                    incident_id=job.incident_id,
                    event_type="investigation.evidence_collection_completed",
                    actor="investigator-worker",
                    from_status=None,
                    to_status=None,
                    details={
                        "job_id": str(job.id),
                        "run_id": str(run.id),
                        "ai_executed": False,
                        "sources": source_summaries,
                    },
                )
        except QueueJobNotFound:
            raise
        except SQLAlchemyError as error:
            raise IncidentStoreUnavailable("could not complete evidence job") from error

    async def record_job_failure(
        self,
        job_id: UUID,
        *,
        error: Exception,
        retry_delay_seconds: int | None,
    ) -> None:
        """Record deterministic retry scheduling or a visible terminal dead letter."""

        now = datetime.now(UTC)
        error_type = type(error).__name__[:128]
        error_message = redact_text(str(error))[:512]
        try:
            async with self._sessions() as session, session.begin():
                job = (
                    await session.execute(
                        select(QueueJobRow).where(QueueJobRow.id == job_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if job is None:
                    raise QueueJobNotFound(str(job_id))
                if QueueJobStatus(job.status) in _TERMINAL_JOB_STATUSES:
                    return
                run = (
                    await session.execute(
                        select(InvestigationRunRow)
                        .where(InvestigationRunRow.id == job.investigation_run_id)
                        .with_for_update()
                    )
                ).scalar_one()
                job.last_error_type = error_type
                job.last_error_message = error_message
                job.lease_expires_at = None
                job.updated_at = now
                run.error_type = error_type
                run.error_message = error_message
                run.updated_at = now
                if retry_delay_seconds is not None and job.attempts < job.max_attempts:
                    job.status = QueueJobStatus.RETRY_SCHEDULED.value
                    job.next_retry_at = now + timedelta(seconds=retry_delay_seconds)
                    run.status = InvestigationRunStatus.RETRY_SCHEDULED.value
                    event_type = "queue.retry_scheduled"
                    details: dict[str, object] = {
                        "job_id": str(job.id),
                        "attempt": job.attempts,
                        "delay_seconds": retry_delay_seconds,
                        "error_type": error_type,
                    }
                else:
                    job.status = QueueJobStatus.DEAD_LETTERED.value
                    job.finished_at = now
                    job.next_retry_at = None
                    run.status = InvestigationRunStatus.DEAD_LETTERED.value
                    run.completed_at = now
                    incident = (
                        await session.execute(
                            select(IncidentRow)
                            .where(IncidentRow.id == job.incident_id)
                            .with_for_update()
                        )
                    ).scalar_one()
                    current_status = IncidentStatus(incident.status)
                    if self._transitions.can_transition(
                        current_status, IncidentStatus.INVESTIGATION_FAILED
                    ):
                        self._transition(
                            session,
                            incident,
                            IncidentStatus.INVESTIGATION_FAILED,
                            actor="investigator-worker",
                            event_type="investigation.failed",
                            now=now,
                        )
                    event_type = "queue.job_dead_lettered"
                    details = {
                        "job_id": str(job.id),
                        "attempts": job.attempts,
                        "error_type": error_type,
                    }
                self._add_audit(
                    session,
                    incident_id=job.incident_id,
                    event_type=event_type,
                    actor="investigator-worker",
                    from_status=None,
                    to_status=None,
                    details=details,
                )
        except QueueJobNotFound:
            raise
        except SQLAlchemyError as store_error:
            raise IncidentStoreUnavailable(
                "could not record investigation failure"
            ) from store_error

    async def list_incidents(
        self,
        *,
        limit: int,
        offset: int,
        status: IncidentStatus | None = None,
    ) -> IncidentPage:
        """Return a stable newest-first page of canonical incidents."""

        occurrences = (
            select(
                AlertOccurrenceRow.incident_id,
                func.count(AlertOccurrenceRow.id).label("occurrence_count"),
            )
            .group_by(AlertOccurrenceRow.incident_id)
            .subquery()
        )
        filters = [IncidentRow.status == status.value] if status is not None else []
        try:
            async with self._sessions() as session:
                total = (
                    await session.execute(select(func.count(IncidentRow.id)).where(*filters))
                ).scalar_one()
                rows = (
                    await session.execute(
                        select(
                            IncidentRow,
                            func.coalesce(occurrences.c.occurrence_count, 0),
                        )
                        .outerjoin(occurrences, occurrences.c.incident_id == IncidentRow.id)
                        .where(*filters)
                        .order_by(IncidentRow.updated_at.desc(), IncidentRow.id.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
        except SQLAlchemyError as error:
            raise IncidentStoreUnavailable("incident listing failed") from error
        return IncidentPage(
            items=[_to_incident_summary(row, int(count)) for row, count in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_incident(self, incident_id: str) -> IncidentDetail | None:
        """Return canonical incident state by public ID."""

        occurrence_count = (
            select(func.count(AlertOccurrenceRow.id))
            .where(AlertOccurrenceRow.incident_id == IncidentRow.id)
            .correlate(IncidentRow)
            .scalar_subquery()
        )
        try:
            async with self._sessions() as session:
                result = (
                    await session.execute(
                        select(IncidentRow, occurrence_count).where(IncidentRow.id == incident_id)
                    )
                ).one_or_none()
        except SQLAlchemyError as error:
            raise IncidentStoreUnavailable("incident lookup failed") from error
        if result is None:
            return None
        row, count = result
        return _to_incident_detail(row, int(count))

    async def list_timeline(
        self,
        incident_id: str,
        *,
        limit: int,
        offset: int,
    ) -> AuditEventPage:
        """Return immutable audit entries in chronological order."""

        filters = [AuditEventRow.incident_id == incident_id]
        try:
            async with self._sessions() as session:
                total = (
                    await session.execute(select(func.count(AuditEventRow.id)).where(*filters))
                ).scalar_one()
                rows = (
                    await session.execute(
                        select(AuditEventRow)
                        .where(*filters)
                        .order_by(AuditEventRow.created_at.asc(), AuditEventRow.id.asc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).scalars()
                items = [_to_audit_event(row) for row in rows]
        except SQLAlchemyError as error:
            raise IncidentStoreUnavailable("incident timeline lookup failed") from error
        return AuditEventPage(items=items, total=total, limit=limit, offset=offset)

    async def list_runs(
        self,
        incident_id: str,
        *,
        limit: int,
        offset: int,
    ) -> InvestigationRunPage:
        """Return newest-first investigation run history for an incident."""

        filters = [InvestigationRunRow.incident_id == incident_id]
        try:
            async with self._sessions() as session:
                total = (
                    await session.execute(
                        select(func.count(InvestigationRunRow.id)).where(*filters)
                    )
                ).scalar_one()
                rows = (
                    await session.execute(
                        select(InvestigationRunRow)
                        .where(*filters)
                        .order_by(
                            InvestigationRunRow.created_at.desc(),
                            InvestigationRunRow.id.desc(),
                        )
                        .limit(limit)
                        .offset(offset)
                    )
                ).scalars()
                items = [_to_investigation_run(row) for row in rows]
        except SQLAlchemyError as error:
            raise IncidentStoreUnavailable("investigation run lookup failed") from error
        return InvestigationRunPage(items=items, total=total, limit=limit, offset=offset)

    async def list_jobs(
        self,
        *,
        limit: int,
        offset: int,
        incident_id: str | None = None,
        status: QueueJobStatus | None = None,
    ) -> QueueJobPage:
        """Return operational queue state, including publish failures and dead letters."""

        filters = []
        if incident_id is not None:
            filters.append(QueueJobRow.incident_id == incident_id)
        if status is not None:
            filters.append(QueueJobRow.status == status.value)
        try:
            async with self._sessions() as session:
                total = (
                    await session.execute(select(func.count(QueueJobRow.id)).where(*filters))
                ).scalar_one()
                rows = (
                    await session.execute(
                        select(QueueJobRow)
                        .where(*filters)
                        .order_by(QueueJobRow.created_at.desc(), QueueJobRow.id.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).scalars()
                items = [_to_queue_job(row) for row in rows]
        except SQLAlchemyError as error:
            raise IncidentStoreUnavailable("queue job lookup failed") from error
        return QueueJobPage(items=items, total=total, limit=limit, offset=offset)

    async def is_ready(self) -> bool:
        """Check only the Incident API/worker's PostgreSQL dependency."""

        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return False
        return True

    async def close(self) -> None:
        """Release pooled database connections."""

        await self._engine.dispose()

    def _transition(
        self,
        session: AsyncSession,
        incident: IncidentRow,
        target: IncidentStatus,
        *,
        actor: str,
        event_type: str,
        now: datetime,
    ) -> None:
        current = IncidentStatus(incident.status)
        validated = self._transitions.transition(current, target)
        if validated == current:
            return
        incident.status = validated.value
        incident.version += 1
        incident.updated_at = now
        self._add_audit(
            session,
            incident_id=incident.id,
            event_type=event_type,
            actor=actor,
            from_status=current,
            to_status=validated,
            details={},
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

    @staticmethod
    def _worker_claim(
        job: QueueJobRow,
        incident: IncidentRow,
        *,
        claimed: bool,
        reason: str,
    ) -> WorkerClaim:
        return WorkerClaim(
            claimed=claimed,
            reason=reason,
            job_id=job.id,
            run_id=job.investigation_run_id,
            incident_id=incident.id,
            incident_title=incident.title,
            service=incident.service,
            affected_services=tuple(incident.affected_services),
            started_at=incident.started_at,
            investigation_window_start=incident.investigation_window_start,
            investigation_window_end=incident.investigation_window_end,
            attempt=job.attempts,
            max_attempts=job.max_attempts,
        )


def _public_incident_id(fingerprint: str) -> str:
    return f"INC-{fingerprint[:16].upper()}"


def _to_incident_summary(row: IncidentRow, occurrence_count: int) -> IncidentSummary:
    return IncidentSummary(
        id=row.id,
        status=IncidentStatus(row.status),
        title=row.title,
        service=row.service,
        affected_services=row.affected_services,
        severity=IncidentSeverity(row.severity),
        started_at=row.started_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        alert_fingerprint=row.alert_fingerprint,
        version=row.version,
        occurrence_count=occurrence_count,
    )


def _to_incident_detail(row: IncidentRow, occurrence_count: int) -> IncidentDetail:
    summary = _to_incident_summary(row, occurrence_count)
    return IncidentDetail(
        **summary.model_dump(),
        investigation_window_start=row.investigation_window_start,
        investigation_window_end=row.investigation_window_end,
        root_cause=row.root_cause,
        confidence=row.confidence,
    )


def _to_audit_event(row: AuditEventRow) -> AuditEventResponse:
    return AuditEventResponse(
        id=row.id,
        incident_id=row.incident_id,
        event_type=row.event_type,
        actor=row.actor,
        from_status=IncidentStatus(row.from_status) if row.from_status is not None else None,
        to_status=IncidentStatus(row.to_status) if row.to_status is not None else None,
        details=row.details,
        created_at=row.created_at,
    )


def _to_investigation_run(row: InvestigationRunRow) -> InvestigationRunResponse:
    return InvestigationRunResponse(
        id=row.id,
        incident_id=row.incident_id,
        stage=row.stage,
        status=InvestigationRunStatus(row.status),
        attempt=row.attempt,
        error_type=row.error_type,
        error_message=row.error_message,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_queue_job(row: QueueJobRow) -> QueueJobResponse:
    return QueueJobResponse(
        id=row.id,
        incident_id=row.incident_id,
        investigation_run_id=row.investigation_run_id,
        status=QueueJobStatus(row.status),
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        last_error_type=row.last_error_type,
        last_error_message=row.last_error_message,
        enqueued_at=row.enqueued_at,
        started_at=row.started_at,
        next_retry_at=row.next_retry_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
