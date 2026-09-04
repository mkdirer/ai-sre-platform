# ADR 0002: Demo checkout and idempotency boundary

- Status: Accepted
- Date: 2026-09-02
- Scope: Milestone 1A / Stage 01 demo request flow

## Context

Milestone 1 needs a small but genuine distributed workload before telemetry is introduced. One
checkout must cross gateway, order, inventory, and payment process boundaries and finish in
PostgreSQL. Network timeouts make the result of a POST ambiguous, so an automatic retry must not
create another order or simulated charge. The slice also needs realistic readiness behavior
without growing into a full commerce system.

## Decision

### Service ownership

- Gateway owns the public `POST /checkout` contract, request correlation, and deterministic order
  identity.
- Order owns synchronous orchestration: reserve inventory, then request payment.
- Inventory owns one configured SKU, availability bound, and price. It is stateless in Stage 01
  and derives a stable reservation ID from order and SKU.
- Payment owns the only durable business boundary. It stores the confirmed transaction/order
  result and exposes read-back by payment ID.

The default Compose deployment runs each FastAPI application in a separate container. Gateway to
order, order to inventory, and order to payment are real HTTP calls. Typed protocols allow these
clients and the payment store to be replaced by deterministic fakes in unit and contract tests.

### Correlation, retry, and idempotency

The public caller must supply a bounded `Idempotency-Key` and may supply a safe `X-Request-ID`.
Gateway generates the request ID when absent and every client preserves both values on downstream
calls and retries.

Outbound calls have an explicit timeout, one to three configured attempts, and bounded linear
backoff. Transport failures and 5xx responses may be retried because the payment boundary is
idempotent. Upstream bodies are schema-validated, and upstream errors map to stable typed public
errors without leaking internal exception detail.

Gateway derives `order_id` from the idempotency key. Inventory derives `reservation_id` from the
order and SKU. Payment computes a canonical SHA-256 fingerprint of its typed request and performs
`INSERT ... ON CONFLICT DO NOTHING` against unique idempotency-key and order identities in
PostgreSQL:

- a new key creates one confirmed row;
- an existing key with the same fingerprint returns the original row and marks the response as a
  replay;
- an existing key with a different fingerprint, or an already-confirmed order under another key,
  returns HTTP 409.

This database operation, not an in-memory cache or the gateway, is the authoritative duplicate
prevention boundary and remains safe when application processes restart or concurrent retries
arrive.

### Schema and health

Alembic is the only production schema-creation path. Application startup never calls
`metadata.create_all`. Compose runs the migration as a one-shot dependency before payment starts.
The migration creates typed columns, unique key/order constraints, and positive value/status
checks.

Liveness means the ASGI process can respond and never checks another component. Readiness checks
only direct required dependencies: gateway → order, order → inventory/payment, payment →
PostgreSQL, and none for inventory. This prevents a service from claiming readiness when its own
request path cannot work while avoiding unrelated transitive checks.

### Container baseline

One locked Python image is reused for all four applications and the migration job. It uses a slim
Python 3.12 base, installs only runtime dependencies, runs as UID/GID 10001, drops Linux
capabilities, forbids privilege escalation, and uses a read-only root filesystem with a bounded
temporary filesystem. PostgreSQL uses a versioned image and a named local volume.

## Consequences

Benefits:

- checkout retries and client retry policy have a durable, testable safety boundary;
- every hop has deterministic contracts and correlation behavior;
- process-level Compose behavior and fast fake-based tests use the same app factories;
- migration, readiness, live-chain, persistence-read, and replay behavior can be demonstrated
  independently.

Tradeoffs and accepted limitations:

- “payment” is a simulated confirmed transaction; no external processor is contacted;
- inventory is a fixed configured catalog bound, not durable stock accounting, and does not need a
  compensation workflow in this milestone;
- the synchronous chain is intentional as a telemetry workload, not a recommendation for a
  production commerce architecture;
- only payment results are durable; a future incident schema must be introduced by separate
  migrations in its assigned milestone;
- the checked-in database password is local-demo-only and must be overridden elsewhere.

## Deferred

Metrics, structured telemetry correlation, traces, logs, alerting, fault injection, queues,
incident persistence, AI, RAG, frontend, Kubernetes, and cloud resources are outside this ADR and
remain assigned to later milestones.
