# Payment Database Runbook

.models/type: runbook
service: payment-service
version: v1

## Symptoms

- `POST /payments` p95 latency exceeds 2 seconds for more than 5 seconds.
- Prometheus alert `DemoPaymentHighLatency` fires with labels
  `service="payment-service"` and `fault="slow_database"`.
- Payment traces show the persistence span dominating total duration.
- Grouped payment logs repeat `slow_database delay injected before persistence`.

## Triage

1. Check `GET /internal/faults/slow-database` for the fault controller state.
2. Compare `metric.service_latency_p95` for `payment-service` against the 2s threshold.
3. Read `log.grouped_patterns` for persistence delay notices.
4. Fetch one slow trace with `trace.slow_service` and confirm the database span.
5. Confirm `deployment.current_previous` shows no coincident version change.

## Mitigation

- Disable the fault with `PUT /internal/faults/slow-database {"enabled": false}`.
- Verify p95 returns below threshold and the alert resolves.
- Do not restart the database before confirming the persistence delay is the cause.
- Record every step with current telemetry evidence IDs; historical similarity alone
  is never sufficient to declare a root cause.

## Escalation

Escalate to the database owner when pool wait queues grow or error rates rise
independently of the injected delay. Attach trace IDs and deployment versions.
