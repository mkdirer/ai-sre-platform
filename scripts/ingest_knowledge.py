"""Deterministic knowledge ingestion for Markdown runbooks and incident history.

Reads knowledge/**/*.md, derives doc_type from the parent directory, and
upserts versioned documents with hash-derived embeddings by default. No paid
API is invoked unless KNOWLEDGE_PROVIDER=openai with an explicit key.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from packages.config import Settings
from packages.models.knowledge import KnowledgeDocType, KnowledgeIngestRequest
from packages.persistence import SqlAlchemyKnowledgeStore
from packages.rag.chunking import chunk_markdown, extract_title
from packages.rag.embeddings import build_embedding_provider
from packages.rag.service import KnowledgeService

_DOC_TYPE_BY_DIR = {
    "runbooks": KnowledgeDocType.RUNBOOK,
    "architecture": KnowledgeDocType.ARCHITECTURE,
    "known_issues": KnowledgeDocType.KNOWN_ISSUE,
    "prior_incidents": KnowledgeDocType.PRIOR_INCIDENT,
}


def _requests(root: Path, version: str) -> list[KnowledgeIngestRequest]:
    requests: list[KnowledgeIngestRequest] = []
    for path in sorted(root.rglob("*.md")):
        parent = path.parent.name
        doc_type = _DOC_TYPE_BY_DIR.get(parent)
        if doc_type is None:
            print(f"skipping {path}: unknown parent directory '{parent}'", file=sys.stderr)
            continue
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            print(f"skipping {path}: empty content", file=sys.stderr)
            continue
        relative = path.relative_to(root).as_posix()
        requests.append(
            KnowledgeIngestRequest(
                source_path=f"knowledge/{relative}",
                doc_type=doc_type,
                version=version,
                title=extract_title(content, fallback=path.stem)[:256],
                content=content,
                doc_metadata={"seed": True, "local_path": f"knowledge/{relative}"},
            )
        )
    return requests


async def _ingest(root: Path, version: str, dry_run: bool) -> int:
    settings = Settings()
    requests = _requests(root, version)
    if dry_run:
        for request in requests:
            chunks = chunk_markdown(
                request.content,
                chunk_tokens=settings.knowledge_chunk_tokens,
                overlap_tokens=settings.knowledge_chunk_overlap_tokens,
            )
            print(f"{request.source_path} type={request.doc_type.value} chunks={len(chunks)}")
        return 0
    provider = build_embedding_provider(settings)
    store = SqlAlchemyKnowledgeStore(settings)
    service = KnowledgeService(settings=settings, store=store, provider=provider)
    try:
        count = 0
        for request in requests:
            response = await service.ingest_markdown(request)
            state = (
                "created" if response.created else ("updated" if response.updated else "unchanged")
            )
            print(f"{request.source_path} {state} chunks={len(response.chunk_ids)}")
            count += 1
        total = await store.count_chunks()
        print(f"ingested {count} documents, {total} total chunks")
        return 0
    finally:
        await provider.close()
        await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Markdown knowledge deterministically")
    parser.add_argument("--root", default="knowledge")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        print(f"knowledge root not found: {root}", file=sys.stderr)
        return 2
    return asyncio.run(_ingest(root, args.version, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
