# ADR 0005: Durable incident ingestion and queue boundary

- Status: accepted
- Date: 2026-09-03
- Scope: repository Stage 04 / implementation-plan Stage 2

## Context

Milestone 1 ended at a bounded in-memory webhook receiver. Stage 2 requires Alertmanager delivery
to become durable and concurrency-safe while keeping the latency-sensitive API independent of
future evidence and AI work. Alertmanager can repeat notifications, multiple API replicas can see
the same payload concurrently, and publication can fail after PostgreSQL commits or succeed before
the API records broker acceptance.

The stage must also demonstrate asynchronous processing truthfully. It cannot label a no-op as a
completed AI investigation, enqueue untrusted full alert bodies, or lose all operational state when
a worker fails.

## Decision

Use PostgreSQL as the canonical state and concurrency boundary:

- compute `alert_fingerprint` as SHA-256 over canonical sorted normalized labels;
- compute a separate delivery fingerprint from alert identity, firing/resolved state, start time,
  and resolved end time; firing ignores advisory end times;
- bound and credential-redact the source-supplied fingerprint retained only as provenance;
- derive stable public incident IDs and internal run/job UUIDs from these fingerprints;
- enforce uniqueness for incident fingerprint, delivery fingerprint, run, and Celery task ID;
- lock the canonical incident row while applying an occurrence and status transition.

One ingestion transaction writes the incident, immutable normalized occurrence, audit entries,
`no_ai_placeholder` run, and a `pending_publish` queue/outbox row. After commit, Incident API sends
a JSON Celery task with exactly one business argument: `incident_id`. It then changes the durable
job to `queued` and returns 202. Broker failure becomes `publish_failed` plus HTTP 503, allowing an
Alertmanager retry to republish the same deterministic task ID. A crash between publish and status
update can cause duplicate delivery, so worker execution must remain idempotent.

Use Redis as the Celery broker/result backend and configure JSON-only serialization, one-message
prefetch, late acknowledgement, rejection on worker loss, an explicit visibility timeout, and a
PostgreSQL job lease. The worker reloads job, run, and canonical incident by ID. It applies the
central status-transition policy, records `placeholder_complete_no_ai` with `ai_executed=false`,
and leaves `root_cause`/`confidence` empty.

Application failures use deterministic bounded exponential backoff. Exhaustion writes
`dead_lettered`, marks the run/incident as failed when allowed, and causes the Celery task itself to
fail. Publish failures and dead letters are queryable from the typed job endpoint. If PostgreSQL is
unavailable and therefore cannot accept a failure record, the Celery result backend is the
equivalent external failure record; the task is never reported as a successful investigation.

Retain the old receiver and its contract tests under the opt-in `test-tools` Compose profile, but
route the normal Alertmanager configuration to Incident API.

## Alternatives considered

- **In-memory deduplication:** rejected because it fails across restarts and replicas.
- **Trust Alertmanager's source fingerprint:** rejected because it is untrusted input and does not
  define this repository's normalization semantics.
- **Publish inside the database transaction:** rejected because PostgreSQL and Redis do not share a
  transaction and a broker wait would hold database locks.
- **Enqueue the full webhook:** rejected because it duplicates untrusted data and permits the worker
  to diverge from canonical state.
- **Claim AI investigation success:** rejected because Stage 2 has no evidence adapters or model.

## Consequences

- Duplicate/concurrent delivery is bounded by database constraints, not deployment topology.
- The transaction/outbox plus deterministic task ID gives at-least-once publication with
  idempotent processing rather than pretending to provide distributed exactly-once semantics.
- Alertmanager receives a retryable 503 when broker acceptance cannot be proven.
- Operators can distinguish pending publication, queued, processing, retry, completion,
  publish-failure, skipped-terminal, and dead-letter states.
- Redis and the worker become local runtime dependencies; only PostgreSQL and Redis affect Incident
  API readiness. Telemetry export remains non-blocking.
- Evidence collection, workflow checkpoints, AI, and remediation remain deferred.
