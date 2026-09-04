# ADR 0006: Deterministic evidence collection boundary

- Status: accepted
- Date: 2026-09-03
- Scope: repository Stage 05 / implementation-plan Stage 3

## Context

Stage 04 durably ingests alerts and queues an explicitly empty worker checkpoint. Stage 3 must turn
an incident into useful metrics, logs, traces, and deployment evidence without granting future
model code a generic PromQL, LogQL, TraceQL, SQL, URL, or shell interface. Telemetry backends may
be empty, delayed, malformed, slow, or independently unavailable. Retried Celery delivery must not
duplicate evidence or erase successful results from another source.

## Decision

Each telemetry integration has a private low-level async HTTP client with a fixed base URL and API
path, explicit timeout, bounded retry policy, and schema validation. It requests identity encoding,
rejects compressed responses, and counts streamed bytes before buffering so the response limit is
an actual memory/network boundary. A separate domain adapter exposes only typed methods from a
closed query-template enum. Service identity is an enum for the five local instrumented services;
trace IDs, time windows, limits, and around-timestamp radii are validated before a query is
rendered. Effective settings such as metric step and slow-trace threshold are persisted as query
parameters and participate in identity. Rendered backend query strings are never persisted or
returned by the Incident API.

Incident scoping uses the canonical service and investigation window stored with the incident. It
rejects unknown services, naive timestamps, excessive lookback, future windows, and windows larger
than configured limits. Telemetry and deployment lookbacks are separate so a deployment preceding
an alert can be correlated without widening every backend query.

Evidence is stored as one canonical row per incident, template, and normalized parameter set. Its
public ID is a SHA-256-derived repository identity over those values. PostgreSQL upserts by that ID,
making retry and late-arriving telemetry updates idempotent while preserving incident ownership.
Every row records source/type, UTC observation time and window, a normalized summary, sanitized
structured payload, template and typed parameters, collection status/error, collection time, and a
payload integrity hash. `empty`, `unavailable`, `failed`, and `timed_out` are separate from
`collected`; none is converted into negative evidence. Conflict updates are monotonic by evidence
quality: a later failure cannot erase an existing `collected` or `empty` result, while a later
successful result can upgrade a prior failure.

The worker runs one bounded task for each source concurrently. Operations within a source also run
concurrently under a single source deadline. Completed operations and explicit failure markers are
persisted in a source-local transaction, so one backend failure cannot discard evidence already
collected from another backend. Only persistence failure retries the durable job; backend failures
are valid partial collection results. Adapter and overall collection duration/outcome are emitted
as bounded metrics, spans, and structured logs. One process-owned worker telemetry runtime exposes
the Prometheus registry on internal port 9464 for scraping and on a loopback-only host binding for
local verification.

Deployment history remains a local PostgreSQL registry. The Incident API accepts immutable,
idempotent, bounded deployment facts and exposes no GitHub client. Its low-level repository offers
only service/time/version reads, while the domain adapter exposes recent deployments,
current/previous versions, and commit/changed-file metadata.

The Incident API exposes incident-isolated evidence and a deterministic correlated timeline.
Timeline event IDs derive from intrinsic trace/deployment identity or canonical normalized log
content, never list positions. Events retain their evidence ID and are sorted by UTC timestamp
followed by stable source and event IDs. Correlation is chronological context, not a causal
conclusion.

## Consequences

- Future investigator code can select only named domain operations and cannot inject query text.
- Partial and missing telemetry remains auditable and safe to retry.
- Fixed templates are coupled intentionally to the demo metric/log/resource schema and must change
  alongside that schema.
- Local deployment metadata is demonstrable without network credentials, but GitHub enrichment is
  deferred.
- Stage 3 completes collection only; it leaves root cause and confidence unset and performs no AI,
  RAG, frontend, or remediation work.
