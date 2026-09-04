# ADR 0008: Versioned knowledge retrieval as supporting context

Status: accepted
Date: 2026-09-05
Scope: Stage 07 / implementation-plan Stage 6

## Context

Telemetry-first investigation (Stages 04–06) is working. Historical runbooks and
prior incidents would help the model, but must never override current evidence
or introduce prompt-injection and ungrounded-citation risks.

## Decision

- Store versioned Markdown in PostgreSQL with pgvector (`knowledge_documents`
  + `knowledge_chunks`, `vector(1536)`, cosine HNSW index, `pgvector/pgvector`
  image).
- Chunk deterministically (600 tokens, 100 overlap), hash content (SHA-256),
  derive stable `DOC-`/`KNW-` IDs, upsert idempotently by source path, replace
  chunks on version/content change, support explicit removal.
- Embed behind an interface: deterministic hash-derived fake offline, OpenAI
  opt-in only with explicit provider selection and key. Validate vector
  dimensions against configuration; reject mismatches.
- Retrieve only through allowlisted runbook/prior-incident/architecture methods
  with metadata filters and bounded `top_k` (default 8).
- Retrieve after telemetry correlation and before hypothesis generation;
  delimit results as untrusted context with size limits, sanitize secrets, and
  cite `KNW-` IDs distinctly from current `EVD-` evidence.
- Enforce telemetry precedence in prompts and deterministic validation:
  historical similarity without current telemetry support cannot select RCA.

## Alternatives considered

- Chroma/Qdrant sidecar: rejected to keep one durable PostgreSQL boundary.
- LLM-generated queries/embeddings inline: rejected; fixed chunking and
  allowlisted retrieval preserve determinism and auditability.
- Knowledge-first RCA: rejected; violates evidence-first invariants.

## Consequences

- New migration `20260905_0005`, settings `KNOWLEDGE_*`, `knowledge/` seeds,
  `scripts/ingest_knowledge.py`, `make ingest-knowledge`, and
  `GET /api/v1/knowledge/search`.
- Tests cover chunking, idempotency, versions, dimensions, filters, ranking,
  citations, empty, unavailable provider, injection, and contradicted-history.
- pgvector image is required for local Compose; offline unit/agent tests use
  in-memory fakes and never call paid APIs.
