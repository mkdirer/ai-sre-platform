# ADR 0004: Controlled fault and alert boundary

- Status: Accepted
- Date: 2026-09-02
- Scope: Milestone 1C / Stage 03 controlled fault and alert path

## Context

Milestone 1 needs one repeatable failure signal without introducing the later Incident API or a
general fault framework. The normal checkout must remain safe by default, the trigger must not be
usable accidentally in production, and acceptance must prove both firing and recovery through the
real Prometheus and Alertmanager APIs.

## Decision

Payment owns one allowlisted `slow_database` controller. Its runtime state is process-local,
lock-protected, and always initialized disabled; there is deliberately no environment setting that
can initialize it enabled. Control requires all three conditions: a development/test environment,
`FAULT_INJECTION_ALLOWED=true`, and a constant-time match of `X-Fault-Control-Token` against a
non-empty secret setting. Compose publishes payment only on loopback and explicitly opts its local
process into control.

When enabled, payment waits the configured fixed `SLOW_DATABASE_DELAY_SECONDS` immediately before
the persistence call. Settings constrain the value to 2–3 seconds. There is no random timing or
failure. Each request span and fault log records the state; OpenTelemetry resource/log fields
continue to supply service version and deployment environment. A fixed-label gauge reports state.

Prometheus alerts when the real payment `POST /payments` p95 histogram over 20 seconds exceeds two
seconds for five seconds. Alertmanager groups the stable alert/service labels and sends firing and
resolved webhooks to a bounded in-memory FastAPI receiver. The receiver is intentionally named and
documented as a stub and exposes only delivery verification/cleanup, avoiding a false Incident API
or persistence claim.

The scenario runner first proves a fast baseline, enables the fault, generates 4–12 unique
checkouts, polls for elevated p95, a firing Prometheus alert, an active Alertmanager alert, and a
firing webhook. An async cleanup context always attempts disable. It then proves a fast checkout,
the disabled gauge, Prometheus recovery, and a resolved webhook. Polling uses monotonic deadlines
and prints diagnostics; fixed sleeps are not acceptance evidence.

## Consequences

- restarting payment clears the fault safely;
- invalid/missing opt-in, token, environment, or delay cannot enable it;
- the alert is derived from user-visible latency rather than the fault-state gauge;
- the local receiver loses history on restart by design;
- later fault scenarios should remain individually allowlisted and must not expand this endpoint
  into arbitrary execution;
- Stage 04 must replace the stub destination with durable Incident API ingestion rather than
  extending the stub.
