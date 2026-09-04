"""Contract for knowledge search isolation from current telemetry evidence."""

import pytest
from httpx import ASGITransport, AsyncClient

from apps.incident_api.main import create_app
from packages.config import Settings
from packages.models.knowledge import KnowledgeDocType, KnowledgeHit, KnowledgePage
from tests.agent.helpers import make_settings


class _FakeKnowledgeStore:
    def __init__(self, page: KnowledgePage) -> None:
        self._page = page

    async def search(
        self,
        query_embedding: list[float],
        *,
        doc_types: list[KnowledgeDocType] | None = None,
        top_k: int,
    ) -> KnowledgePage:
        items = list(self._page.items)
        if doc_types is not None:
            items = [hit for hit in items if hit.doc_type in doc_types]
        return KnowledgePage(items=items[:top_k], total=len(items), limit=top_k, offset=0)

    async def close(self) -> None:
        return None


class _FakeEmbedder:
    dimensions = 16

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 16 for _ in texts]

    async def close(self) -> None:
        return None


def _hit() -> KnowledgeHit:
    return KnowledgeHit(
        chunk_id="KNW-AAAAAAAAAAAAAAAAAAAAAAAA",  # type: ignore[arg-type]
        document_id="DOC-AAAAAAAAAAAAAAAAAAAA",  # type: ignore[arg-type]
        source_path="knowledge/runbooks/payment.md",
        doc_type=KnowledgeDocType.RUNBOOK,
        version="v1",
        title="Payment runbook",
        chunk_index=0,
        text="persistence delay guidance",
        similarity=0.9,
        distance=0.1,
    )


async def _client() -> AsyncClient:
    settings: Settings = make_settings()
    page = KnowledgePage(items=[_hit()], total=1, limit=8, offset=0)
    app = create_app(
        settings,
        store=_FakeIncidentStore(),
        publisher=_FakePublisher(),
        queue_dependency=_FakeQueue(),
        evidence_store=_FakeEvidence(),
        knowledge_store=_FakeKnowledgeStore(page),  # type: ignore[arg-type]
        knowledge_embedder=_FakeEmbedder(),  # type: ignore[arg-type]
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _FakeIncidentStore:
    async def get_incident(self, incident_id: str) -> None:
        return None

    async def list_incidents(self, *, limit: int, offset: int, status=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def list_timeline(self, incident_id: str, *, limit: int, offset: int):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def list_runs(self, incident_id: str, *, limit: int, offset: int):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def list_jobs(self, *, limit: int, offset: int, incident_id=None, status=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def ingest(self, alerts):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def mark_job_published(self, job_id) -> None:  # type: ignore[no-untyped-def]
        return None

    async def mark_job_publish_failed(self, job_id, error) -> None:  # type: ignore[no-untyped-def]
        return None

    async def is_ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class _FakePublisher:
    async def publish(self, *, job_id, incident_id) -> None:  # type: ignore[no-untyped-def]
        return None


class _FakeQueue:
    async def is_ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class _FakeEvidence:
    async def list_evidence(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def all_evidence(self, incident_id: str):  # type: ignore[no-untyped-def]
        return ()

    async def register_deployment(self, registration):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def list_deployments(self, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_knowledge_search_returns_citations_distinct_from_evidence() -> None:
    client = await _client()
    try:
        response = await client.get("/api/v1/knowledge/search", params={"q": "payment latency"})
        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["chunk_id"].startswith("KNW-")
        assert body["items"][0]["doc_type"] == "runbook"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_knowledge_search_respects_doc_type_filter() -> None:
    client = await _client()
    try:
        response = await client.get(
            "/api/v1/knowledge/search",
            params={"q": "payment", "doc_type": "prior_incident"},
        )
        assert response.status_code == 200
        assert response.json()["items"] == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_knowledge_search_rejects_blank_query() -> None:
    client = await _client()
    try:
        response = await client.get("/api/v1/knowledge/search", params={"q": "   "})
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_knowledge_query"
    finally:
        await client.aclose()
