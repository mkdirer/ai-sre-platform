# Stage 05 — deterministic evidence collection adapters

Implement Stage 3 from the plan without adding an LLM or LangGraph.

Create typed, async, read-only adapters for Prometheus, Loki, Tempo, and the local deployment store. Each adapter must have a low-level client plus allowlisted domain methods. Implement only fixed query templates with typed parameters; do not expose arbitrary PromQL, LogQL, SQL, URL, or query strings to future model code.

Required domain capabilities include:

- metrics: service latency, error rate, CPU, memory, DB pool usage when instrumented;
- logs: errors, patterns or safely grouped messages, logs around a timestamp;
- traces: trace by ID, slow service traces, service dependency evidence;
- deployments: recent deployments, current/previous version, commit/changed-file metadata from the local store.

Add canonical evidence models and persistence. Every item needs stable ID, incident ownership, source/type, UTC timestamp/window, normalized summary, structured payload, query template/parameters, and collection status. Make collection idempotent. Represent unavailable/empty/failed sources distinctly.

Implement deterministic incident scoping and timeline correlation. Add the worker step that collects all source types concurrently with per-source timeouts and persists partial results safely. Instrument adapter calls and collection duration/errors.

Register demo deployments through the API and update the bad-deployment scenario metadata if needed, but do not integrate real GitHub yet.

Tests must use realistic fixtures/fake servers and cover bounds, timeouts, malformed responses, no data, partial failure, stable IDs, incident isolation, query injection attempts, and timeline ordering. Add an integration test that collects real local evidence for `slow_database` and exposes it through the Incident API.

Do not add OpenAI, LangGraph, RAG, frontend, or remediation. Run full applicable checks and report evidence collected from each real local source. Do not commit or start Stage 06.
