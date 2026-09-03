"""Typed local deployment client and allowlisted evidence domain methods."""

from datetime import datetime
from typing import Protocol

from packages.models.deployments import (
    DeploymentAtQuery,
    DeploymentCommitQuery,
    DeploymentEnvironment,
    DeploymentRecord,
    DeploymentWindowQuery,
)
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceService,
    EvidenceSource,
    EvidenceType,
    QueryTemplate,
)
from packages.persistence import EvidenceStoreUnavailable
from packages.tools.http import AdapterUnavailableError


class DeploymentReader(Protocol):
    """Fixed low-level persistence reads available to the deployment client."""

    async def recent_deployments(
        self,
        *,
        service: EvidenceService,
        environment: DeploymentEnvironment,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> tuple[DeploymentRecord, ...]: ...

    async def current_previous_deployments(
        self,
        *,
        service: EvidenceService,
        environment: DeploymentEnvironment,
        at: datetime,
    ) -> tuple[DeploymentRecord, ...]: ...

    async def get_deployment(
        self,
        *,
        deployment_id: str,
        service: EvidenceService,
    ) -> DeploymentRecord | None: ...


class DeploymentClient:
    """Low-level client exposing named repository reads and no SQL strings."""

    def __init__(self, reader: DeploymentReader) -> None:
        self._reader = reader

    async def recent(self, request: DeploymentWindowQuery) -> tuple[DeploymentRecord, ...]:
        try:
            return await self._reader.recent_deployments(
                service=request.service,
                environment=request.environment,
                start=request.window.start,
                end=request.window.end,
                limit=request.limit,
            )
        except EvidenceStoreUnavailable as error:
            raise AdapterUnavailableError("deployment store is unavailable") from error

    async def current_previous(
        self,
        request: DeploymentAtQuery,
    ) -> tuple[DeploymentRecord, ...]:
        try:
            return await self._reader.current_previous_deployments(
                service=request.service,
                environment=request.environment,
                at=request.at,
            )
        except EvidenceStoreUnavailable as error:
            raise AdapterUnavailableError("deployment store is unavailable") from error

    async def commit_metadata(
        self,
        request: DeploymentCommitQuery,
    ) -> DeploymentRecord | None:
        try:
            return await self._reader.get_deployment(
                deployment_id=request.deployment_id,
                service=request.service,
            )
        except EvidenceStoreUnavailable as error:
            raise AdapterUnavailableError("deployment store is unavailable") from error


class DeploymentAdapter:
    """Allowlisted deployment history/version/commit evidence methods."""

    def __init__(self, client: DeploymentClient) -> None:
        self._client = client

    async def get_recent_deployments(self, request: DeploymentWindowQuery) -> EvidenceDraft:
        records = await self._client.recent(request)
        parameters = _window_parameters(request)
        if not records:
            return EvidenceDraft(
                source=EvidenceSource.DEPLOYMENT_STORE,
                type=EvidenceType.DEPLOYMENT,
                status=CollectionStatus.EMPTY,
                observed_at=request.window.end,
                window=request.window,
                summary=f"No recent deployments were registered for {request.service.value}",
                payload={},
                query_template=QueryTemplate.DEPLOYMENT_RECENT,
                query_parameters=parameters,
                provenance={"adapter": "local_deployment_store", "storage": "postgresql"},
            )
        return EvidenceDraft(
            source=EvidenceSource.DEPLOYMENT_STORE,
            type=EvidenceType.DEPLOYMENT,
            status=CollectionStatus.COLLECTED,
            observed_at=max(record.deployed_at for record in records),
            window=request.window,
            summary=(
                f"Found {len(records)} recent deployments for {request.service.value}; "
                f"latest version is {records[0].version}"
            ),
            payload={"deployments": [record.model_dump(mode="json") for record in records]},
            query_template=QueryTemplate.DEPLOYMENT_RECENT,
            query_parameters=parameters,
            provenance={"adapter": "local_deployment_store", "storage": "postgresql"},
        )

    async def get_current_previous_version(self, request: DeploymentAtQuery) -> EvidenceDraft:
        records = await self._client.current_previous(request)
        parameters: dict[str, object] = {
            "service": request.service.value,
            "environment": request.environment.value,
            "at": request.at.isoformat(),
            "window_start": request.window.start.isoformat(),
            "window_end": request.window.end.isoformat(),
        }
        if not records:
            return EvidenceDraft(
                source=EvidenceSource.DEPLOYMENT_STORE,
                type=EvidenceType.DEPLOYMENT,
                status=CollectionStatus.EMPTY,
                observed_at=request.at,
                window=request.window,
                summary=f"No current deployment was registered for {request.service.value}",
                payload={},
                query_template=QueryTemplate.DEPLOYMENT_CURRENT_PREVIOUS,
                query_parameters=parameters,
                provenance={"adapter": "local_deployment_store", "storage": "postgresql"},
            )
        current = records[0]
        previous = records[1] if len(records) > 1 else None
        previous_text = previous.version if previous is not None else "unknown"
        return EvidenceDraft(
            source=EvidenceSource.DEPLOYMENT_STORE,
            type=EvidenceType.DEPLOYMENT,
            status=CollectionStatus.COLLECTED,
            observed_at=current.deployed_at,
            window=request.window,
            summary=(
                f"Current {request.service.value} version is {current.version}; "
                f"previous version is {previous_text}"
            ),
            payload={
                "current": current.model_dump(mode="json"),
                "previous": previous.model_dump(mode="json") if previous is not None else None,
            },
            query_template=QueryTemplate.DEPLOYMENT_CURRENT_PREVIOUS,
            query_parameters=parameters,
            provenance={"adapter": "local_deployment_store", "storage": "postgresql"},
        )

    async def get_commit_metadata(self, request: DeploymentCommitQuery) -> EvidenceDraft:
        record = await self._client.commit_metadata(request)
        parameters: dict[str, object] = {
            "service": request.service.value,
            "deployment_id": request.deployment_id,
            "window_start": request.window.start.isoformat(),
            "window_end": request.window.end.isoformat(),
        }
        if record is None:
            return EvidenceDraft(
                source=EvidenceSource.DEPLOYMENT_STORE,
                type=EvidenceType.DEPLOYMENT,
                status=CollectionStatus.EMPTY,
                observed_at=request.window.end,
                window=request.window,
                summary=(
                    f"Deployment {request.deployment_id} was not registered for "
                    f"{request.service.value}"
                ),
                payload={},
                query_template=QueryTemplate.DEPLOYMENT_COMMIT_METADATA,
                query_parameters=parameters,
                provenance={"adapter": "local_deployment_store", "storage": "postgresql"},
            )
        return EvidenceDraft(
            source=EvidenceSource.DEPLOYMENT_STORE,
            type=EvidenceType.DEPLOYMENT,
            status=CollectionStatus.COLLECTED,
            observed_at=record.deployed_at,
            window=request.window,
            summary=(
                f"Deployment {record.id} uses commit {record.commit_sha[:12]} "
                f"and changes {len(record.changed_files)} files"
            ),
            payload={
                "deployment_id": record.id,
                "commit_sha": record.commit_sha,
                "changed_files": record.changed_files,
                "metadata": record.metadata,
            },
            query_template=QueryTemplate.DEPLOYMENT_COMMIT_METADATA,
            query_parameters=parameters,
            provenance={"adapter": "local_deployment_store", "storage": "postgresql"},
        )


def _window_parameters(request: DeploymentWindowQuery) -> dict[str, object]:
    return {
        "service": request.service.value,
        "environment": request.environment.value,
        "window_start": request.window.start.isoformat(),
        "window_end": request.window.end.isoformat(),
        "deployment_limit": request.limit,
    }
