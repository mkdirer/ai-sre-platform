"""Async persistence for hypotheses, reports, recommendations, calls, and failures."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.config import Settings
from packages.models.evidence import EvidenceService
from packages.models.investigation import (
    AdditionalEvidenceRequest,
    Hypothesis,
    HypothesisPage,
    HypothesisStatus,
    IncidentReport,
    ModelCallRecord,
    Recommendation,
    RecommendationAction,
    RecommendationPage,
    RecommendationRisk,
    RootCauseCategory,
    RunUsage,
)
from packages.persistence.database import create_database_engine
from packages.persistence.investigation_rows import (
    HypothesisRow,
    IncidentReportRow,
    InvestigationFailureRow,
    InvestigatorCallRow,
    RecommendationRow,
)
from packages.telemetry import redact_text, redact_value


class InvestigationStoreUnavailable(Exception):
    """PostgreSQL could not safely complete an investigator artifact operation."""


class SqlAlchemyInvestigationStore:
    """Idempotent durable artifact store used by the graph and Incident API."""

    def __init__(self, settings: Settings, *, engine: AsyncEngine | None = None) -> None:
        self._engine = engine or create_database_engine(settings)
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def save_hypotheses(
        self,
        run_id: UUID,
        incident_id: str,
        hypotheses: Sequence[Hypothesis],
    ) -> None:
        """Upsert stable hypotheses without weakening incident ownership."""

        now = datetime.now(UTC)
        try:
            async with self._sessions() as session, session.begin():
                for hypothesis in hypotheses:
                    if hypothesis.incident_id != incident_id:
                        raise ValueError("hypothesis incident ownership mismatch")
                    values = {
                        "id": hypothesis.id,
                        "incident_id": incident_id,
                        "run_id": run_id,
                        "category": hypothesis.category.value,
                        "description": hypothesis.description,
                        "status": hypothesis.status.value,
                        "confidence": hypothesis.confidence,
                        "supporting_evidence_ids": list(hypothesis.supporting_evidence_ids),
                        "contradicting_evidence_ids": list(hypothesis.contradicting_evidence_ids),
                        "reasoning_summary": hypothesis.reasoning_summary,
                        "next_evidence_requests": [
                            request.model_dump(mode="json")
                            for request in hypothesis.next_evidence_requests
                        ],
                        "created_at": now,
                        "updated_at": now,
                    }
                    statement = insert(HypothesisRow).values(**values)
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[HypothesisRow.id],
                            set_={
                                key: value
                                for key, value in values.items()
                                if key not in {"id", "incident_id", "run_id", "created_at"}
                            },
                            where=(
                                (HypothesisRow.incident_id == incident_id)
                                & (HypothesisRow.run_id == run_id)
                            ),
                        )
                    )
        except ValueError:
            raise
        except SQLAlchemyError as error:
            raise InvestigationStoreUnavailable("hypothesis persistence failed") from error

    async def save_report(self, run_id: UUID, report: IncidentReport) -> None:
        """Persist one stable report and its non-executable recommendations."""

        now = datetime.now(UTC)
        report_values = {
            "id": report.id,
            "incident_id": report.incident_id,
            "run_id": run_id,
            "status": report.status.value,
            "root_cause": report.root_cause.value if report.root_cause is not None else None,
            "confidence": report.confidence,
            "report": report.model_dump(mode="json"),
            "generated_at": report.generated_at,
            "created_at": now,
            "updated_at": now,
        }
        try:
            async with self._sessions() as session, session.begin():
                statement = insert(IncidentReportRow).values(**report_values)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[IncidentReportRow.id],
                        set_={
                            key: value
                            for key, value in report_values.items()
                            if key not in {"id", "incident_id", "run_id", "created_at"}
                        },
                        where=(
                            (IncidentReportRow.incident_id == report.incident_id)
                            & (IncidentReportRow.run_id == run_id)
                        ),
                    )
                )
                for recommendation in report.recommendations:
                    values = {
                        "id": recommendation.id,
                        "incident_id": report.incident_id,
                        "run_id": run_id,
                        "report_id": report.id,
                        "action_type": recommendation.action_type.value,
                        "target": recommendation.target.value,
                        "parameters": recommendation.parameters,
                        "rationale_evidence_ids": list(recommendation.rationale_evidence_ids),
                        "risk": recommendation.risk.value,
                        "reversible": recommendation.reversible,
                        "requires_approval": recommendation.requires_approval,
                        "status": recommendation.status,
                        "created_at": now,
                        "updated_at": now,
                    }
                    recommendation_statement = insert(RecommendationRow).values(**values)
                    await session.execute(
                        recommendation_statement.on_conflict_do_update(
                            index_elements=[RecommendationRow.id],
                            set_={
                                key: value
                                for key, value in values.items()
                                if key
                                not in {"id", "incident_id", "run_id", "report_id", "created_at"}
                            },
                            where=(
                                (RecommendationRow.incident_id == report.incident_id)
                                & (RecommendationRow.run_id == run_id)
                                & (RecommendationRow.report_id == report.id)
                            ),
                        )
                    )
        except SQLAlchemyError as error:
            raise InvestigationStoreUnavailable("report persistence failed") from error

    async def record_call(self, record: ModelCallRecord) -> None:
        """Persist bounded provider/tool metadata without prompt or response bodies."""

        values = {
            "id": record.id,
            "run_id": UUID(record.run_id),
            "incident_id": record.incident_id,
            "kind": record.kind,
            "operation": record.operation,
            "provider": record.provider,
            "model": record.model,
            "status": record.status,
            "attempt": record.attempt,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "estimated_cost_usd": record.estimated_cost_usd,
            "duration_seconds": record.duration_seconds,
            "error_type": record.error_type,
            "error_message": record.error_message,
            "call_metadata": redact_value(record.metadata),
            "created_at": record.created_at,
        }
        try:
            async with self._sessions() as session, session.begin():
                statement = insert(InvestigatorCallRow).values(**values)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[InvestigatorCallRow.id],
                        set_={key: value for key, value in values.items() if key != "id"},
                        where=(
                            (InvestigatorCallRow.incident_id == record.incident_id)
                            & (InvestigatorCallRow.run_id == UUID(record.run_id))
                        ),
                    )
                )
        except (SQLAlchemyError, ValueError) as error:
            raise InvestigationStoreUnavailable("call metadata persistence failed") from error

    async def usage_for_run(self, run_id: UUID) -> RunUsage:
        """Restore persisted budget usage before a retry or checkpoint resume."""

        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        select(
                            func.count(InvestigatorCallRow.id).filter(
                                InvestigatorCallRow.kind == "model"
                            ),
                            func.count(InvestigatorCallRow.id).filter(
                                InvestigatorCallRow.kind == "tool"
                            ),
                            func.coalesce(func.sum(InvestigatorCallRow.input_tokens), 0),
                            func.coalesce(func.sum(InvestigatorCallRow.output_tokens), 0),
                            func.coalesce(func.sum(InvestigatorCallRow.estimated_cost_usd), 0.0),
                        ).where(InvestigatorCallRow.run_id == run_id)
                    )
                ).one()
        except SQLAlchemyError as error:
            raise InvestigationStoreUnavailable("run usage lookup failed") from error
        return RunUsage(
            model_calls=int(row[0]),
            tool_calls=int(row[1]),
            input_tokens=int(row[2]),
            output_tokens=int(row[3]),
            estimated_cost_usd=float(row[4]),
        )

    async def record_failure(
        self,
        *,
        failure_id: str,
        run_id: UUID,
        incident_id: str,
        stage: str,
        error: BaseException,
        details: dict[str, object] | None = None,
    ) -> None:
        """Idempotently retain a safe workflow-level failure record."""

        values = {
            "id": failure_id,
            "run_id": run_id,
            "incident_id": incident_id,
            "stage": stage[:64],
            "error_type": type(error).__name__[:128],
            "error_message": redact_text(str(error))[:512],
            "details": redact_value(details or {}),
            "created_at": datetime.now(UTC),
        }
        try:
            async with self._sessions() as session, session.begin():
                await session.execute(
                    insert(InvestigationFailureRow)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[InvestigationFailureRow.id])
                )
        except SQLAlchemyError as store_error:
            raise InvestigationStoreUnavailable("failure persistence failed") from store_error

    async def get_latest_report(self, incident_id: str) -> IncidentReport | None:
        """Return the newest canonical report for an incident."""

        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        select(IncidentReportRow)
                        .where(IncidentReportRow.incident_id == incident_id)
                        .order_by(
                            IncidentReportRow.generated_at.desc(), IncidentReportRow.id.desc()
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
        except SQLAlchemyError as error:
            raise InvestigationStoreUnavailable("report lookup failed") from error
        return IncidentReport.model_validate(row.report) if row is not None else None

    async def list_hypotheses(self, incident_id: str, *, limit: int, offset: int) -> HypothesisPage:
        """Return incident-isolated hypotheses in stable order."""

        filters = [HypothesisRow.incident_id == incident_id]
        try:
            async with self._sessions() as session:
                total = (
                    await session.execute(select(func.count(HypothesisRow.id)).where(*filters))
                ).scalar_one()
                rows = (
                    await session.execute(
                        select(HypothesisRow)
                        .where(*filters)
                        .order_by(HypothesisRow.created_at.desc(), HypothesisRow.id.asc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).scalars()
                items = [_hypothesis_from_row(row) for row in rows]
        except SQLAlchemyError as error:
            raise InvestigationStoreUnavailable("hypothesis lookup failed") from error
        return HypothesisPage(items=items, total=total, limit=limit, offset=offset)

    async def list_recommendations(
        self, incident_id: str, *, limit: int, offset: int
    ) -> RecommendationPage:
        """Return persisted proposals without providing an execution endpoint."""

        filters = [RecommendationRow.incident_id == incident_id]
        try:
            async with self._sessions() as session:
                total = (
                    await session.execute(select(func.count(RecommendationRow.id)).where(*filters))
                ).scalar_one()
                rows = (
                    await session.execute(
                        select(RecommendationRow)
                        .where(*filters)
                        .order_by(RecommendationRow.created_at.desc(), RecommendationRow.id.asc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).scalars()
                items = [_recommendation_from_row(row) for row in rows]
        except SQLAlchemyError as error:
            raise InvestigationStoreUnavailable("recommendation lookup failed") from error
        return RecommendationPage(items=items, total=total, limit=limit, offset=offset)

    async def close(self) -> None:
        """Release the owned SQLAlchemy connection pool."""

        await self._engine.dispose()


def _hypothesis_from_row(row: HypothesisRow) -> Hypothesis:
    return Hypothesis(
        id=row.id,
        incident_id=row.incident_id,
        category=RootCauseCategory(row.category),
        description=row.description,
        status=HypothesisStatus(row.status),
        confidence=row.confidence,
        supporting_evidence_ids=row.supporting_evidence_ids,
        contradicting_evidence_ids=row.contradicting_evidence_ids,
        reasoning_summary=row.reasoning_summary,
        next_evidence_requests=[
            AdditionalEvidenceRequest.model_validate(item) for item in row.next_evidence_requests
        ],
    )


def _recommendation_from_row(row: RecommendationRow) -> Recommendation:
    return Recommendation(
        id=row.id,
        action_type=RecommendationAction(row.action_type),
        target=EvidenceService(row.target),
        parameters=row.parameters,
        rationale_evidence_ids=row.rationale_evidence_ids,
        risk=RecommendationRisk(row.risk),
        reversible=row.reversible,
        requires_approval=row.requires_approval,
        status=row.status,
    )
