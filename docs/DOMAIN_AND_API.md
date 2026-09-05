# Domain model and API contracts

## Stage 01 checkout contracts

All request and response bodies reject unknown fields. IDs are UUIDs unless described otherwise;
timestamps are UTC-aware ISO 8601 values; money is represented as positive integer cents.

### Common headers and errors

- `Idempotency-Key` is required on every Stage 01 POST. It is 1–128 safe ASCII characters.
- `X-Request-ID` is optional at the gateway and generated when absent. If supplied, it is 1–64
  safe ASCII characters. The same value is propagated and echoed across the request chain.
- Instrumented requests accept and propagate the W3C `traceparent` header. Successful responses
  echo the active 32-character lowercase trace ID in `X-Trace-ID`; it is correlation metadata, not
  part of the checkout body or an idempotency boundary.
- Expected failures use `{"code":"...","message":"...","request_id":"..."}`. Invalid bodies
  return 422 and conflicting identity reuse returns 409. Direct connection failures return 503,
  direct timeouts return 504, and exhausted upstream 5xx or invalid responses map to 502.

### Gateway

`POST /checkout` accepts:

```json
{"customer_id": "customer-1", "sku": "widget-001", "quantity": 2}
```

It returns the confirmed `payment_id`, `order_id`, `reservation_id`, original business fields,
`unit_price_cents`, `total_cents`, `status`, `created_at`, `request_id`, and
`idempotent_replay`. The gateway creates a deterministic order ID from the idempotency key and
calls the order service over HTTP.

### Internal demo APIs

- order service: `POST /orders` accepts the checkout fields plus `order_id`;
- inventory service: `POST /reservations` accepts `order_id`, `sku`, and `quantity` and returns a
  deterministic reservation plus the authoritative configured price;
- payment service: `POST /payments` accepts the order/reservation/customer/item/price fields and
  atomically creates or reads one confirmed payment;
- payment service: `GET /payments/{payment_id}` reads a stored payment for verification.

Each service exposes `GET /health/live` and `GET /health/ready`. Readiness is scoped to direct
dependencies as described in [Architecture](ARCHITECTURE.md).

## Incident platform entities

Incident, evidence, run/job, occurrence, audit, and deployment contracts are implemented through
Stage 05. Hypothesis, recommendation, and report contracts are implemented in Stage 06 and
persisted by the investigator worker; Stage 08 exposes them through Incident API reads plus
human approve/reject decisions recorded in `approvals`.

### Incident

Required fields:

- `id`: stable public ID such as `INC-000042`;
- `status`;
- `title`;
- `service` and `affected_services`;
- `severity`;
- `started_at`, `created_at`, `completed_at`;
- `investigation_window_start`, `investigation_window_end`;
- `root_cause` nullable;
- `confidence` in `[0, 1]`;
- `alert_fingerprint`;
- `version` for optimistic concurrency.

### Evidence

Every evidence item contains:

- stable `id` (`EVD-` for current telemetry, `KNW-` for historical knowledge citations);
- `incident_id`;
- `type`: metric, log, trace, or deployment in Stage 05; Stage 07 adds historical knowledge
  context that is stored separately, retrieved after correlation, and cited distinctly;
- `source`;
- UTC timestamp or explicit time window;
- short normalized summary;
- structured payload;
- collection query/template name and parameters;
- collection status: `collected`, `empty`, `unavailable`, `failed`, or `timed_out`, with a bounded
  typed error for failure states;
- payload SHA-256 and source/API provenance metadata.

Parameters include effective configured values such as the Prometheus range step and Tempo
minimum duration. Stable IDs therefore change when query semantics change. Idempotent upserts are
monotonic by evidence quality: `collected` cannot be replaced by `empty` or a failure, and `empty`
cannot be replaced by a failure from a later retry.

An unavailable data source is represented explicitly. It is not equivalent to evidence that an event did not occur.

### Hypothesis

Required fields:

- `id`;
- `description`;
- `candidate_root_cause`;
- `status`: proposed, verified, rejected, inconclusive;
- `confidence`;
- `supporting_evidence_ids`;
- `contradicting_evidence_ids`;
- `reasoning_summary`;
- `next_evidence_requests`.

### Recommendation

Required fields:

- `id`;
- `action_type` from an allowlist;
- `target`;
- structured parameters;
- `rationale` with evidence IDs;
- `risk`;
- `reversible`;
- `requires_approval` always true for mutation;
- approval/execution/verification status.

### IncidentReport

The final model output is schema-validated and includes:

- incident ID and title;
- affected services and severity;
- summary;
- nullable root cause;
- confidence;
- timeline events;
- hypotheses;
- evidence references (current `EVD-` telemetry only);
- knowledge references (historical `KNW-` chunk citations, supporting context only);
- recommendations;
- related incidents;
- limitations/data gaps;
- status.

Historical similarity never proves the current root cause: every causal claim must
still be corroborated by current telemetry evidence IDs.

## API endpoints

### Demo

- Stage 01: checkout, internal order/reservation/payment, payment read-back, liveness, and
  readiness endpoints documented above.
- Stage 02: every demo service exposes `GET /metrics` in Prometheus text format. The stable metric
  families are `demo_http_requests_total`, `demo_http_request_errors_total`,
  `demo_http_request_duration_seconds`, `demo_http_requests_in_progress`, and
  `demo_fault_enabled`. Labels are restricted to fixed service/method/route/status/error/fault
  dimensions; request, trace, idempotency, and business identifiers are forbidden.
- Stage 03 payment control: `GET /internal/faults/slow-database` reads state and
  `PUT /internal/faults/slow-database` accepts exactly `{"enabled": true|false}`. Both require
  `X-Fault-Control-Token`; the process must also be explicitly opted in and running in `development`
  or `test`. State always initializes disabled and cannot be enabled from an environment value.
- Stage 09 extended faults: payment additionally exposes allowlisted
  `GET/PUT /internal/faults/{pool-exhaustion,bad-deployment,cpu-saturation,high-error-rate}`
  plus `GET /internal/faults` listing, and inventory exposes
  `GET/PUT /internal/faults/inventory-timeout` plus `GET /internal/faults`. All share the
  Stage 03 auth, always-off, bounded-delay, and reversible guarantees; `cpu_saturation` is a
  short simulated delay with a tiny bounded hash loop (no host burn) and `high_error_rate`
  is a deterministic hash-based simulated 500. Unknown or cross-service names return 404.
- Stage 03 receiver test tool: `POST /webhooks/alertmanager` validates a bounded Alertmanager v4
  webhook and returns 202. `GET /deliveries` provides optional `alertname`/`status` filters and
  `DELETE /deliveries` clears only the disposable in-memory verification store. These endpoints
  provide no durable ingestion semantics. The receiver remains under the explicit Compose
  `test-tools` profile and is no longer an Alertmanager runtime target.

### Incident API (Stage 05 / implementation-plan Stages 2–3)

- `POST /api/v1/alerts` — bounded Alertmanager v4 webhook; persists and enqueues, then returns 202.
- `GET /api/v1/incidents?limit=25&offset=0&status=queued` — stable newest-first summary page.
- `GET /api/v1/incidents/{incident_id}` — canonical current state. Stage 05 intentionally returns
  nullable `root_cause` and `confidence`; it does not claim an AI report exists.
- `GET /api/v1/incidents/{incident_id}/timeline?limit=50&offset=0` — chronological audit page.
- `GET /api/v1/incidents/{incident_id}/investigation-runs?limit=25&offset=0` — named deterministic
  evidence run and retry status.
- `GET /api/v1/incidents/{incident_id}/evidence?limit=50&offset=0&source=tempo&status=collected`
  — incident-isolated canonical evidence, with optional enum filters.
- `GET /api/v1/incidents/{incident_id}/evidence/timeline?limit=50&offset=0` — deterministic
  evidence-derived chronological context.
- `POST /api/v1/deployments` — validate and idempotently register immutable local deployment
  metadata; returns 201 with `created=false` on an exact replay and 409 on conflicting metadata.
- `GET /api/v1/deployments?service=payment-service&environment=development&limit=25&offset=0`
  — newest-first local deployment metadata.
- `GET /api/v1/investigation-jobs?incident_id=...&status=dead_lettered&limit=25&offset=0` —
  operational queue, publish-failure, retry, and dead-letter visibility.
- `GET /api/v1/knowledge/search?q=...&doc_type=runbook&top_k=8` — bounded cosine retrieval
  over versioned historical context (`top_k` default 8, effective max 20 via
  `KNOWLEDGE_MAX_TOP_K`; the API accepts up to 50 and clamps), with `KNW-` chunk citations kept distinct from current
  `EVD-` evidence. Returns 503 when knowledge search is not configured.
- `GET /health/live`, `GET /health/ready`, `GET /metrics`; the worker's process-owned Prometheus
  registry is available separately on loopback port 9464 and is scraped internally.

The ingestion response contains one result per alert: `incident_id`, repository-owned
`alert_fingerprint`, current status, whether an occurrence was recorded, whether the delivery was
a duplicate, and whether an investigation was enqueued. Required alert identity labels are
`alertname` and a safe `service`; `severity` defaults to `warning` and is normalized to
`info|warning|critical`. Firing alerts ignore an advisory `endsAt`, while resolved updates preserve
their real end time. The occurrence key hashes fingerprint, status, normalized start, and resolved
end time. Source-supplied fingerprints are bounded to 256 characters, credential-redacted,
retained only as provenance, and never trusted for deduplication.

All expected errors use the common typed envelope and request ID. Invalid payload/identity and
pagination return 422, unknown incidents return 404, persistence or broker unavailability returns
503. List limits are 1–100 and offset is 0–100000. Request, alert, incident, and job IDs never
become Prometheus labels.

Human approval (Stage 08) is API-owned and audited:

- `GET /api/v1/incidents/{incident_id}/report` — newest report or 404 `report_absent`.
- `GET /api/v1/incidents/{incident_id}/hypotheses` — competing hypotheses with
  rejected reasons; `GET .../recommendations` — risk and approval state.
- `GET /api/v1/knowledge/chunks/{chunk_id}` — one chunk for related-knowledge
  display, or 404 `knowledge_chunk_not_found`.
- `POST /api/v1/recommendations/{id}/approve|reject` — body
  `{"incident_version": N, "actor": "..."}` plus required `Idempotency-Key`.
  Decisions require a `waiting_for_approval` recommendation and incident;
  stale versions, conflicting replays, and wrong states return 409
  (`stale_version`, `approval_conflict`, `not_awaiting_approval`,
  `invalid_state`). Replays with the same key and decision return the stored
  record with `replayed: true`. Approval keeps the incident paused in
  `waiting_for_approval` and never executes remediation; rejection moves it to
  `rejected`. Every decision writes an audit event with actor and timestamp.

Approved remediation (Stage 10) resumes from the approval pause:

- `POST /api/v1/recommendations/{id}/execute` — body
  `{"incident_version": N, "expected_service_version": "0.2.0", "actor": "..."}`
  plus required `Idempotency-Key`; returns 202 with the execution record.
  Claims one execution row per approved recommendation, revalidates the exact
  incident version and registered deployment versions, moves the incident
  `waiting_for_approval → remediating`, and enqueues the worker task. Stale,
  mismatched, forbidden, concurrent, or already-completed requests return 409
  (`stale_version`, `invalid_state`, `forbidden_action`,
  `execution_in_progress`, `already_completed`); same-key replays return the
  stored record with `replayed: true`. Queue outage fails the claim visibly
  (503 `queue_unavailable`) instead of stranding it.
- `GET /api/v1/remediations/{id}` — execution status, attempts, and result.
- `POST /api/v1/remediations/{id}/stop` — body
  `{"incident_version": N, "actor": "..."}` plus required `Idempotency-Key`;
  flags the execution; the worker observes the flag between verification
  polls and ends unresolved.
- The worker executes via the allowlisted payment rollback adapter
  (`remediating → verifying`), verifies deterministic p95 recovery over a
  bounded window, and resolves only on verified recovery
  (`verifying → resolved`); ambiguity, failure, or stop leave the incident
  unresolved with gaps. Unknown adapter outcomes require read-back
  confirmation and never count as success.

No API accepts raw PromQL, LogQL, TraceQL, SQL, shell commands, or arbitrary
telemetry URLs.

## Persistence tables

Stage 01 owns:

- `checkout_payments`: confirmed payment/order result, unique idempotency key, unique order ID,
  request fingerprint, positive quantity/price/total constraints, and UTC creation timestamp.

Stage 04 adds:

- `incidents`: one canonical row per repository-owned alert fingerprint;
- `alert_occurrences`: immutable normalized firing/resolved updates with a unique delivery hash;
- `investigation_runs`: explicit run stage and retry/terminal status; historical Stage 04 rows use
  `no_ai_placeholder`, while new work uses `evidence_collection`;
- `queue_jobs`: transactional outbox plus publish, lease, retry, completion, and dead-letter state;
- `audit_events`: immutable ingestion, transition, queue, and worker history.

Stage 05 adds:

- `evidence`: incident-owned source/type/status/window/payload/template/provenance rows with stable
  IDs and integrity hashes;
- `deployments`: immutable local service/environment/version/commit history;
- `evidence_collection` and `evidence_collected` run states while retaining historical Stage 04
  rows.

Stage 06 adds:

- `hypotheses`, `incident_reports`, `recommendations`, `investigator_calls`,
  `investigation_failures`, and LangGraph checkpoint tables;
- `ai_investigation` run stage with `report_generated`, `waiting_for_approval`, and
  `insufficient_evidence` states.

Stage 07 adds:

- `knowledge_documents` (versioned source path/type/version/hash/metadata) and
  `knowledge_chunks` (ordered text, `vector(1536)` embedding, HNSW cosine index);
- `knowledge_references` (`KNW-`) on `IncidentReport`, kept distinct from
  `evidence_references` (`EVD-`).

Stage 08 adds:

- `approvals` (one immutable decision per recommendation with actor, incident
  version, idempotency key, and timestamp);
- `approved`/`rejected` recommendation states.

Stage 10 adds:

- `remediation_executions` (one lifeline per approved recommendation with
  action, target, incident version, idempotency key, attempts, stop flag,
  and result; terminal `completed` rejects re-execution);
- `remediating` → `verifying` → `resolved` incident progression driven only
  by the execution service, with `investigation_failed` for failure/stop.

Later stages add:

- `workflow_checkpoints` or LangGraph-compatible checkpoint storage.

Use JSONB only for genuinely variable payloads. Queryable identity, status, timestamps, relationships, and confidence values stay in typed columns.

## Confidence policy

- Confidence is a calibrated output signal, not decorative UI.
- A hypothesis without supporting evidence IDs cannot exceed `0.30`.
- `root_cause` requires at least one supporting evidence item and no unresolved strong contradiction.
- If required data sources fail or evidence remains ambiguous, use `root_cause = null` and list the gaps.
- Thresholds are configuration and evaluated against scenarios; do not hard-code them throughout the codebase.

## Evidence-first invariant

The following must be mechanically validated before storing a final report:

1. Every referenced evidence ID exists and belongs to the incident.
2. Every root-cause assertion is linked to one or more evidence IDs.
3. Rejected hypotheses contain contradiction evidence or a clear lack-of-support result.
4. Model output cannot invent a deployment, metric, log line, trace, service, or timestamp not present in collected context.
5. Unknown data remains unknown.
