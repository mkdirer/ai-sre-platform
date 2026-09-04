# Prior Incident: High CPU on Order Service (2026-07-02)

.models/type: prior_incident
service: order-service
version: v1
incident: INC-20260702-CPU

## Summary

Order service CPU saturation caused elevated checkout latency without database
involvement. `metric.service_cpu` showed sustained saturation while
`metric.service_latency_p95` for `payment-service` remained nominal. Payment
persistence spans were short and pool metrics were healthy.

## Evidence pattern

- `metric.service_cpu` saturation on `order-service`.
- Nominal payment latency and no persistence delay logs.
- Traces showed compute spans, not database spans, dominating duration.

## Resolution

Traffic was shed and the hot loop was fixed. This incident is unrelated to
payment database latency and must not be cited as evidence for a database
root cause.

## Lesson

CPU saturation elsewhere can coincide with payment alerts. Require
payment-service telemetry before attributing latency to the database.
