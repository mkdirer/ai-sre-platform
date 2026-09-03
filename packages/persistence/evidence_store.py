"""Async PostgreSQL persistence for canonical evidence and local deployments."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from pydantic import JsonValue
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.config import Settings
from packages.models.deployments import (
    DeploymentEnvironment,
    DeploymentPage,
    DeploymentRecord,
    DeploymentRegistration,
    DeploymentRegistrationResponse,
)
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceItem,
    EvidencePage,
    EvidenceService,
    EvidenceSource,
    EvidenceType,
    EvidenceWindow,
    QueryTemplate,
)
from packages.persistence.database import create_database_engine
from packages.persistence.evidence_rows import DeploymentRow, EvidenceRow
from packages.telemetry import redact_value


class EvidenceStoreUnavailable(Exception):
    """PostgreSQL could not safely complete an evidence operation."""


class DeploymentConflict(Exception):
    """A stable deployment identity was reused with different metadata."""


class SqlAlchemyEvidenceStore:
    """Idempotent evidence store and allowlisted local deployment repository."""

    def __init__(self, settings: Settings, *, engine: AsyncEngine | None = None) -> None:
        self._engine = engine or create_database_engine(settings)
        self._correlation_limit = settings.evidence_correlation_limit
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def persist_evidence(
        self,
        incident_id: str,
        drafts: Sequence[EvidenceDraft],
        *,
        collected_at: datetime | None = None,
    ) -> tuple[EvidenceItem, ...]:
        """Upsert a source-local batch by stable ID and return canonical rows."""

        if not drafts:
            return ()
        now = (collected_at or datetime.now(UTC)).astimezone(UTC)
        ids: list[str] = []
        try:
            async with self._sessions() as session, session.begin():
                for draft in drafts:
                    payload = _safe_json_object(draft.payload)
                    parameters = _safe_json_object(draft.query_parameters)
                    provenance = _safe_json_object(draft.provenance)
                    evidence_id = stable_evidence_id(
                        incident_id=incident_id,
                        source=draft.source,
                        query_template=draft.query_template.value,
                        query_parameters=parameters,
                    )
                    ids.append(evidence_id)
                    values: dict[str, object] = {
                        "id": evidence_id,
                        "incident_id": incident_id,
                        "source": draft.source.value,
                        "evidence_type": draft.type.value,
                        "status": draft.status.value,
                        "observed_at": draft.observed_at,
                        "window_start": draft.window.start,
                        "window_end": draft.window.end,
                        "summary": draft.summary,
                        "payload": payload,
                        "query_template": draft.query_template.value,
                        "query_parameters": parameters,
                        "provenance": provenance,
                        "error_type": draft.error_type,
                        "error_message": draft.error_message,
                        "payload_sha256": _sha256(payload),
                        "collected_at": now,
                        "created_at": now,
                        "updated_at": now,
                    }
                    update_values = {
                        key: value
                        for key, value in values.items()
                        if key not in {"id", "incident_id", "created_at"}
                    }
                    statement = insert(EvidenceRow).values(**values)
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[EvidenceRow.id],
                            set_=update_values,
                            where=(
                                case(
                                    (
                                        statement.excluded.status
                                        == CollectionStatus.COLLECTED.value,
                                        2,
                                    ),
                                    (statement.excluded.status == CollectionStatus.EMPTY.value, 1),
                                    else_=0,
                                )
                                >= case(
                                    (EvidenceRow.status == CollectionStatus.COLLECTED.value, 2),
                                    (EvidenceRow.status == CollectionStatus.EMPTY.value, 1),
                                    else_=0,
                                )
                            ),
                        )
                    )
                rows = (
                    await session.execute(
                        select(EvidenceRow)
                        .where(
                            EvidenceRow.incident_id == incident_id,
                            EvidenceRow.id.in_(ids),
                        )
                        .order_by(EvidenceRow.observed_at.asc(), EvidenceRow.id.asc())
                    )
                ).scalars()
                items = tuple(_to_evidence(row) for row in rows)
        except SQLAlchemyError as error:
            raise EvidenceStoreUnavailable("evidence persistence failed") from error
        if len(items) != len(set(ids)):
            raise EvidenceStoreUnavailable("evidence identity ownership mismatch")
        return items

    async def list_evidence(
        self,
        incident_id: str,
        *,
        limit: int,
        offset: int,
        source: EvidenceSource | None = None,
        status: CollectionStatus | None = None,
    ) -> EvidencePage:
        """Return one incident's evidence in stable chronological order."""

        filters = [EvidenceRow.incident_id == incident_id]
        if source is not None:
            filters.append(EvidenceRow.source == source.value)
        if status is not None:
            filters.append(EvidenceRow.status == status.value)
        try:
            async with self._sessions() as session:
                total = (
                    await session.execute(select(func.count(EvidenceRow.id)).where(*filters))
                ).scalar_one()
                rows = (
                    await session.execute(
                        select(EvidenceRow)
                        .where(*filters)
                        .order_by(EvidenceRow.observed_at.asc(), EvidenceRow.id.asc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).scalars()
                items = [_to_evidence(row) for row in rows]
        except SQLAlchemyError as error:
            raise EvidenceStoreUnavailable("evidence listing failed") from error
        return EvidencePage(items=items, total=total, limit=limit, offset=offset)

    async def all_evidence(self, incident_id: str) -> tuple[EvidenceItem, ...]:
        """Load the bounded Stage 3 evidence set used for timeline correlation."""

        try:
            async with self._sessions() as session:
                rows = list(
                    (
                        await session.execute(
                            select(EvidenceRow)
                            .where(EvidenceRow.incident_id == incident_id)
                            .order_by(EvidenceRow.observed_at.asc(), EvidenceRow.id.asc())
                            .limit(self._correlation_limit + 1)
                        )
                    ).scalars()
                )
        except SQLAlchemyError as error:
            raise EvidenceStoreUnavailable("evidence correlation read failed") from error
        if len(rows) > self._correlation_limit:
            raise EvidenceStoreUnavailable("incident evidence exceeds the correlation bound")
        return tuple(_to_evidence(row) for row in rows)

    async def register_deployment(
        self,
        registration: DeploymentRegistration,
    ) -> DeploymentRegistrationResponse:
        """Idempotently register one immutable local deployment fact."""

        sanitized = registration.model_copy(
            update={"metadata": _safe_json_object(registration.metadata)}
        )
        deployment_id = stable_deployment_id(sanitized)
        now = datetime.now(UTC)
        values = {
            "id": deployment_id,
            "service": sanitized.service.value,
            "environment": sanitized.environment.value,
            "version": sanitized.version,
            "deployed_at": sanitized.deployed_at,
            "commit_sha": sanitized.commit_sha,
            "changed_files": list(sanitized.changed_files),
            "deployment_metadata": sanitized.metadata,
            "registered_at": now,
        }
        try:
            async with self._sessions() as session, session.begin():
                created_id = (
                    await session.execute(
                        insert(DeploymentRow)
                        .values(**values)
                        .on_conflict_do_nothing(index_elements=[DeploymentRow.id])
                        .returning(DeploymentRow.id)
                    )
                ).scalar_one_or_none()
                row = (
                    await session.execute(
                        select(DeploymentRow).where(DeploymentRow.id == deployment_id)
                    )
                ).scalar_one()
                record = _to_deployment(row)
                if record.model_dump(exclude={"id", "registered_at"}) != sanitized.model_dump():
                    raise DeploymentConflict(
                        "deployment identity already exists with different metadata"
                    )
        except DeploymentConflict:
            raise
        except SQLAlchemyError as error:
            raise EvidenceStoreUnavailable("deployment registration failed") from error
        return DeploymentRegistrationResponse(deployment=record, created=created_id is not None)

    async def list_deployments(
        self,
        *,
        limit: int,
        offset: int,
        service: EvidenceService | None = None,
        environment: DeploymentEnvironment | None = None,
    ) -> DeploymentPage:
        """Return bounded local deployment metadata without accepting SQL."""

        filters = []
        if service is not None:
            filters.append(DeploymentRow.service == service.value)
        if environment is not None:
            filters.append(DeploymentRow.environment == environment.value)
        try:
            async with self._sessions() as session:
                total = (
                    await session.execute(select(func.count(DeploymentRow.id)).where(*filters))
                ).scalar_one()
                rows = (
                    await session.execute(
                        select(DeploymentRow)
                        .where(*filters)
                        .order_by(DeploymentRow.deployed_at.desc(), DeploymentRow.id.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).scalars()
                items = [_to_deployment(row) for row in rows]
        except SQLAlchemyError as error:
            raise EvidenceStoreUnavailable("deployment listing failed") from error
        return DeploymentPage(items=items, total=total, limit=limit, offset=offset)

    async def recent_deployments(
        self,
        *,
        service: EvidenceService,
        environment: DeploymentEnvironment,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> tuple[DeploymentRecord, ...]:
        """Read a service's deployments from one explicit bounded time window."""

        try:
            async with self._sessions() as session:
                rows = (
                    await session.execute(
                        select(DeploymentRow)
                        .where(
                            DeploymentRow.service == service.value,
                            DeploymentRow.environment == environment.value,
                            DeploymentRow.deployed_at >= start,
                            DeploymentRow.deployed_at <= end,
                        )
                        .order_by(DeploymentRow.deployed_at.desc(), DeploymentRow.id.desc())
                        .limit(limit)
                    )
                ).scalars()
                return tuple(_to_deployment(row) for row in rows)
        except SQLAlchemyError as error:
            raise EvidenceStoreUnavailable("recent deployment lookup failed") from error

    async def current_previous_deployments(
        self,
        *,
        service: EvidenceService,
        environment: DeploymentEnvironment,
        at: datetime,
    ) -> tuple[DeploymentRecord, ...]:
        """Read the latest deployment of the current and previous distinct versions."""

        try:
            async with self._sessions() as session:
                latest_per_version = (
                    select(
                        DeploymentRow.id.label("deployment_id"),
                        func.row_number()
                        .over(
                            partition_by=DeploymentRow.version,
                            order_by=(
                                DeploymentRow.deployed_at.desc(),
                                DeploymentRow.id.desc(),
                            ),
                        )
                        .label("version_rank"),
                    )
                    .where(
                        DeploymentRow.service == service.value,
                        DeploymentRow.environment == environment.value,
                        DeploymentRow.deployed_at <= at,
                    )
                    .subquery()
                )
                rows = (
                    await session.execute(
                        select(DeploymentRow)
                        .join(
                            latest_per_version,
                            latest_per_version.c.deployment_id == DeploymentRow.id,
                        )
                        .where(
                            latest_per_version.c.version_rank == 1,
                        )
                        .order_by(DeploymentRow.deployed_at.desc(), DeploymentRow.id.desc())
                        .limit(2)
                    )
                ).scalars()
                return tuple(_to_deployment(row) for row in rows)
        except SQLAlchemyError as error:
            raise EvidenceStoreUnavailable("deployment version lookup failed") from error

    async def get_deployment(
        self,
        *,
        deployment_id: str,
        service: EvidenceService,
    ) -> DeploymentRecord | None:
        """Read commit metadata only when the deployment belongs to the typed service."""

        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        select(DeploymentRow).where(
                            DeploymentRow.id == deployment_id,
                            DeploymentRow.service == service.value,
                        )
                    )
                ).scalar_one_or_none()
        except SQLAlchemyError as error:
            raise EvidenceStoreUnavailable("deployment metadata lookup failed") from error
        return _to_deployment(row) if row is not None else None

    async def close(self) -> None:
        """Release pooled database connections."""

        await self._engine.dispose()


def stable_evidence_id(
    *,
    incident_id: str,
    source: EvidenceSource,
    query_template: str,
    query_parameters: Mapping[str, object],
) -> str:
    """Derive an incident-owned identity independent of result arrival or retry time."""

    digest = _sha256(
        {
            "incident_id": incident_id,
            "source": source.value,
            "query_template": query_template,
            "query_parameters": query_parameters,
        }
    )
    return f"EVD-{digest[:24].upper()}"


def stable_deployment_id(registration: DeploymentRegistration) -> str:
    """Derive a stable identity from immutable deployment coordinates."""

    digest = _sha256(
        {
            "service": registration.service.value,
            "environment": registration.environment.value,
            "version": registration.version,
            "deployed_at": registration.deployed_at.isoformat(),
            "commit_sha": registration.commit_sha,
        }
    )
    return f"DEP-{digest[:20].upper()}"


def _sha256(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _safe_json_object(value: Mapping[str, object]) -> dict[str, object]:
    # Backend response bytes and adapter result counts are bounded before this point. Evidence
    # payloads legitimately nest samples/spans below the shallower structured-logging limit.
    redacted = redact_value(value, max_depth=8, max_collection_items=10_000)
    if not isinstance(redacted, dict):
        raise ValueError("structured evidence must be a JSON object")
    return cast(dict[str, object], redacted)


def _to_evidence(row: EvidenceRow) -> EvidenceItem:
    return EvidenceItem(
        id=row.id,
        incident_id=row.incident_id,
        source=EvidenceSource(row.source),
        type=EvidenceType(row.evidence_type),
        status=CollectionStatus(row.status),
        observed_at=row.observed_at,
        window=EvidenceWindow(start=row.window_start, end=row.window_end),
        summary=row.summary,
        payload=row.payload,
        query_template=QueryTemplate(row.query_template),
        query_parameters=row.query_parameters,
        provenance=row.provenance,
        error_type=row.error_type,
        error_message=row.error_message,
        payload_sha256=row.payload_sha256,
        collected_at=row.collected_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_deployment(row: DeploymentRow) -> DeploymentRecord:
    return DeploymentRecord(
        id=row.id,
        service=EvidenceService(row.service),
        environment=DeploymentEnvironment(row.environment),
        version=row.version,
        deployed_at=row.deployed_at,
        commit_sha=row.commit_sha,
        changed_files=row.changed_files,
        metadata=cast(dict[str, JsonValue], row.deployment_metadata),
        registered_at=row.registered_at,
    )
