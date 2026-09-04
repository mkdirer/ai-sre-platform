# Stage 08 — focused frontend and human approval

Implement only Stage 7 from the plan. Build a small React + TypeScript + Vite frontend with two primary views:

1. Incident list with severity, service, status, timestamps, and confidence/data-gap indicator.
2. Incident detail with RCA/insufficient-evidence state, correlated timeline, evidence cards with provenance, competing hypotheses including rejected reasons, related knowledge, recommendation risk, and audit status.

Add approve/reject controls only for recommendations in `waiting_for_approval`. API-side approval must be the source of truth and include recommendation/incident version checks, actor placeholder suitable for local demo, timestamp, and audit event. Handle stale/replayed requests idempotently and show actionable errors. Approval in this stage resumes the graph/state to an explicit approved state but must not execute remediation yet.

Keep UI dependencies small and accessible. Provide loading, empty, unavailable-source, insufficient-evidence, error, and stale-update states. Do not fabricate data for the real runtime; fixtures may be used only in tests/story-like development assets clearly separated from production.

Add typed API client/contracts generated from or checked against OpenAPI where practical. Add component tests and API integration tests for list/detail/approve/reject, permission/state validation, replay, and concurrency. Add a minimal E2E browser test for reviewing and approving a recommendation.

Update Docker Compose, health checks, README, and screenshots instructions. Do not implement remediation, Kubernetes, Terraform, or cloud deployment. Run Python and frontend gates plus clean Compose verification. Do not commit or start Stage 09.
