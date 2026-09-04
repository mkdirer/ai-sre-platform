"""Strict contracts for versioned knowledge documents, chunks, and retrieval."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

DocumentId = Annotated[str, StringConstraints(pattern=r"^DOC-[A-F0-9]{20}$")]
ChunkId = Annotated[str, StringConstraints(pattern=r"^KNW-[A-F0-9]{24}$")]
KnowledgeTitle = Annotated[str, StringConstraints(min_length=1, max_length=256)]
KnowledgeText = Annotated[str, StringConstraints(min_length=1, max_length=32_000)]


class KnowledgeDocType(StrEnum):
    """Allowlisted knowledge source types."""

    RUNBOOK = "runbook"
    ARCHITECTURE = "architecture"
    KNOWN_ISSUE = "known_issue"
    PRIOR_INCIDENT = "prior_incident"


class KnowledgeDocument(BaseModel):
    """One versioned Markdown source document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: DocumentId
    source_path: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    doc_type: KnowledgeDocType
    version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    content_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    title: KnowledgeTitle
    doc_metadata: dict[str, object] = Field(default_factory=dict)
    chunk_count: Annotated[int, Field(ge=0)] = 0
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("knowledge document timestamps must include a timezone")
        return value.astimezone(UTC)


class KnowledgeChunk(BaseModel):
    """One ordered chunk with a validated embedding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ChunkId
    document_id: DocumentId
    source_path: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    doc_type: KnowledgeDocType
    version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    chunk_index: Annotated[int, Field(ge=0)]
    text: KnowledgeText
    embedding: Annotated[list[float], Field(min_length=1)]
    token_estimate: Annotated[int, Field(ge=0)]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("knowledge chunk timestamp must include a timezone")
        return value.astimezone(UTC)


class KnowledgeQuery(BaseModel):
    """Bounded retrieval input; only allowlisted doc types are accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    doc_types: Annotated[list[KnowledgeDocType], Field(max_length=4)] | None = None
    top_k: Annotated[int, Field(ge=1, le=50)] = 8


class KnowledgeHit(BaseModel):
    """One ranked, citation-ready retrieval result (untrusted data)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: ChunkId
    document_id: DocumentId
    source_path: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    doc_type: KnowledgeDocType
    version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    title: KnowledgeTitle
    chunk_index: Annotated[int, Field(ge=0)]
    text: Annotated[str, StringConstraints(min_length=1, max_length=8_000)]
    similarity: Annotated[float, Field(ge=-1, le=1)]
    distance: Annotated[float, Field(ge=0)]


class KnowledgePage(BaseModel):
    """Bounded retrieval page with explicit total."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[KnowledgeHit]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=50)]
    offset: Annotated[int, Field(ge=0)]


class KnowledgeIngestRequest(BaseModel):
    """Deterministic ingestion input for one Markdown document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_path: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    doc_type: KnowledgeDocType
    version: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "v1"
    title: KnowledgeTitle
    content: Annotated[str, StringConstraints(min_length=1, max_length=200_000)]
    doc_metadata: dict[str, object] = Field(default_factory=dict)


class KnowledgeIngestResponse(BaseModel):
    """Idempotent ingestion outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document: KnowledgeDocument
    created: bool
    updated: bool
    chunk_ids: list[ChunkId]
