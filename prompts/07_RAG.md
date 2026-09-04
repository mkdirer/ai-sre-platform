# Stage 07 — knowledge ingestion and RAG

Implement only Stage 6 from `docs/IMPLEMENTATION_PLAN.md`. Preserve the existing telemetry-first investigation; RAG is supporting context, never the primary source of current incident facts.

Add PostgreSQL pgvector storage and migrations for versioned knowledge documents/chunks. Implement deterministic ingestion for Markdown runbooks, architecture docs, known issues, and prior incident reports with:

- source path/type, document version/hash, timestamps, metadata, and chunk order;
- idempotent re-ingestion and removal/update behavior;
- configurable chunk size around 500–800 tokens and overlap around 100;
- configured embedding provider/model behind an interface and deterministic fake embeddings in tests;
- vector dimension derived from or validated against configuration rather than silently assumed;
- cosine similarity and an HNSW index;
- metadata filters and bounded `top_k` default around 8;
- document/chunk citations retained through the final report.

Create allowlisted knowledge methods for runbooks, prior incidents, and architecture documents. Retrieved content is untrusted data: delimit it, apply size limits, and ensure instructions inside a document cannot alter tools, policy, or workflow. Historical similarity must not be presented as proof of the current root cause.

Integrate retrieval after current telemetry correlation and before final hypothesis generation/verification. Clearly distinguish current evidence from historical context in domain models and UI/API output.

Seed several realistic documents, including a relevant DB-pool incident, an unrelated high-CPU incident, a runbook, and an adversarial document containing prompt-injection-like text. Do not encode current scenario answers in system prompts.

Add unit/integration/agent tests for chunking, hashing/idempotency, version updates, vector dimension mismatch, metadata filters, ranking, citations, empty results, unavailable embedding provider, malicious content, and a case where history is similar but current telemetry contradicts it.

Update docs and ingestion commands. Run all deterministic checks; do not invoke a paid embedding/model API without explicit approval. Do not commit or start Stage 08.
