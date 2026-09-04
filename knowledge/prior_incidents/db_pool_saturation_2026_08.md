# Prior Incident: DB Pool Saturation (2026-08-14)

.models/type: prior_incident
service: payment-service
version: v1
incident: INC-20260814-POOL

## Summary

Payment p95 latency rose to 3.1 seconds during a load test. `metric.db_pool_usage`
showed pool wait queues growing while `metric.service_latency_p95` breached the
2s threshold. Logs contained `pool exhausted, waiting for connection` and traces
showed queueing before the persistence span.

## Evidence pattern

- `metric.db_pool_usage` with `pool` saturation markers.
- `log.grouped_patterns` with connection-wait samples.
- `trace.slow_service` with queue time dominating duration.
- No coincident deployment version change.

## Resolution

Pool size was increased and idle timeouts tuned. Recovery was verified with
telemetry thresholds, not with historical similarity.

## Lesson

Pool saturation and injected `slow_database` delay both raise p95 latency. The
distinguishing signal is pool wait telemetry. Always corroborate historical
similarity with current pool metrics before claiming this root cause.
