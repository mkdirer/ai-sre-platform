"""Embedding provider interface with deterministic fake and OpenAI backends."""

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol

from packages.config import Settings


class EmbeddingProviderUnavailable(RuntimeError):
    """The configured embedding provider cannot serve a request."""


class EmbeddingDimensionMismatch(ValueError):
    """An embedding vector does not match the configured dimension."""


class EmbeddingProvider(Protocol):
    """Replaceable embedding boundary; production and deterministic fakes."""

    @property
    def dimensions(self) -> int: ...

    @property
    def name(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def close(self) -> None: ...


def validate_dimensions(vectors: Sequence[Sequence[float]], *, expected: int) -> None:
    """Reject empty or dimension-mismatched embedding batches."""

    if not vectors:
        raise EmbeddingDimensionMismatch("embedding batch is empty")
    for vector in vectors:
        if len(vector) != expected:
            raise EmbeddingDimensionMismatch(
                f"embedding dimension {len(vector)} != configured {expected}"
            )


class DeterministicFakeEmbeddingProvider:
    """Hash-derived deterministic embeddings for tests and offline ingestion.

    Vectors are L2-normalized so cosine similarity is a dot product. Output
    depends only on input text and configured dimensions.
    """

    def __init__(self, dimensions: int = 1536) -> None:
        if dimensions < 8 or dimensions > 4096:
            raise ValueError("fake embedding dimensions out of range")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return "fake"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vectors.append(self.embed_one(text))
        validate_dimensions(vectors, expected=self._dimensions)
        return vectors

    def embed_one(self, text: str) -> list[float]:
        raw: list[float] = []
        for index in range(self._dimensions):
            digest = hashlib.sha256(f"{text}\x00{index}".encode()).digest()
            # Map 8 bytes to [-1, 1].
            value = int.from_bytes(digest[:8], "big") / (2**64 - 1) * 2.0 - 1.0
            raw.append(value)
        norm = math.sqrt(sum(item * item for item in raw)) or 1.0
        return [item / norm for item in raw]

    async def close(self) -> None:
        return None


class UnavailableEmbeddingProvider:
    """Deterministic failure double for unavailable-provider tests."""

    def __init__(self, dimensions: int = 1536) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return "unavailable"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingProviderUnavailable("embedding provider is unavailable")

    async def close(self) -> None:
        return None


class OpenAIEmbeddingProvider:
    """Production OpenAI embeddings backend (never invoked without explicit opt-in)."""

    def __init__(self, settings: Settings) -> None:
        api_key = settings.openai_api_key.get_secret_value()
        if not api_key:
            raise EmbeddingProviderUnavailable("OPENAI_API_KEY is required for embeddings")
        if settings.knowledge_provider != "openai":
            raise EmbeddingProviderUnavailable("knowledge provider is not configured to openai")
        # Imported lazily so offline/test environments never require network creds.
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key)
        self._model = settings.knowledge_embedding_model
        self._dimensions = settings.knowledge_embedding_dimensions
        self._timeout = settings.knowledge_embedding_timeout_seconds

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return "openai"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            raise EmbeddingDimensionMismatch("embedding batch is empty")
        response = await self._client.embeddings.create(
            model=self._model,
            input=list(texts),
            timeout=self._timeout,
        )
        vectors = [list(item.embedding) for item in response.data]
        validate_dimensions(vectors, expected=self._dimensions)
        return vectors

    async def close(self) -> None:
        await self._client.close()


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Select the configured provider without performing network I/O."""

    if settings.knowledge_provider == "openai":
        return OpenAIEmbeddingProvider(settings)
    if settings.knowledge_provider == "fake":
        return DeterministicFakeEmbeddingProvider(
            dimensions=settings.knowledge_embedding_dimensions
        )
    raise EmbeddingProviderUnavailable(
        f"unsupported knowledge provider: {settings.knowledge_provider}"
    )


__all__ = [
    "DeterministicFakeEmbeddingProvider",
    "EmbeddingDimensionMismatch",
    "EmbeddingProvider",
    "EmbeddingProviderUnavailable",
    "OpenAIEmbeddingProvider",
    "UnavailableEmbeddingProvider",
    "build_embedding_provider",
    "validate_dimensions",
]
