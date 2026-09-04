# Stage 04 — durable Incident API and asynchronous queue

Implement Stage 2 from `docs/IMPLEMENTATION_PLAN.md`. Replace the temporary webhook receiver only after preserving its test value.

Required implementation:

- FastAPI Incident API with typed endpoints from `docs/DOMAIN_AND_API.md` relevant to this stage;
- normalized Alertmanager webhook parsing supporting firing and resolved updates;
- stable fingerprinting, idempotency, and deduplication under repeated/concurrent delivery;
- PostgreSQL tables/migrations for incidents, alert occurrences, investigation runs, audit events, and minimal queue tracking;
- Redis and Celery worker infrastructure;
- webhook transaction persists canonical state and enqueues by incident ID, then returns HTTP 202 quickly;
- worker loads canonical data by ID and performs a no-AI placeholder state transition that is explicitly named, observable, and safe to retry;
- status-transition service enforcing allowed transitions;
- pagination, typed errors, health/readiness, metrics, and structured logs with incident ID;
- dead-letter/failure behavior or a documented equivalent, retry/backoff policy, and visibility into failed jobs.

Do not run LLM work in the API request, enqueue full alert payloads, or hide failures as successful investigations. Do not implement telemetry adapters or AI yet.

Add unit and integration tests for parsing, fingerprinting, duplicate/concurrent delivery, status transitions, transaction/queue boundary, retry idempotency, and API contracts. Reconfigure Alertmanager to target the real API and extend the E2E smoke flow: fault → alert → one durable incident → queued/processed placeholder investigation.

Run migrations from empty and prior revision, full applicable checks, and clean Compose verification. Update docs and OpenAPI examples. Do not commit or start Stage 05.
