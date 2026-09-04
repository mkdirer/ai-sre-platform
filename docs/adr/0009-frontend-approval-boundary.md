# ADR 0009: Focused frontend and human-approval boundary

Status: accepted
Date: 2026-09-06
Scope: Stage 08 / implementation-plan Stage 7

## Context

Reports, hypotheses, and recommendations are persisted but have no read API or
review surface. Operators need two focused screens and a durable human-approval
pause before any future remediation stage.

## Decision

- Build a small React + TypeScript + Vite app (`apps/frontend`) with incident
  list and incident detail views only. Serve it from Compose via nginx with a
  same-origin `/api` proxy to incident-api and a `/health/live` healthcheck.
- The API is the source of truth: add `GET report/hypotheses/recommendations`
  reads, `GET knowledge/chunks/{id}` for related knowledge, and
  `POST recommendations/{id}/approve|reject` with `ApprovalRequest`
  (`incident_version`, demo `actor`), required `Idempotency-Key`, and
  concurrency-safe `SELECT … FOR UPDATE` decisions in a new `approvals` table
  (one decision per recommendation, replay-safe, stale/conflict → 409).
- Approval records the decision and audit events but never executes
  remediation: approve keeps the incident in `waiting_for_approval` (the
  durable pause); reject moves it to `rejected`. The LangGraph is not
  re-entered; the recorded decision resumes state for a later stage.
- Keep UI dependencies to react, react-dom, react-router-dom. Cover loading,
  empty, unavailable-source, insufficient-evidence, error, and stale-update
  states. Fixtures live only in test/story assets, never in production paths.
- Check the hand-written typed client against OpenAPI in contract tests.

## Alternatives considered

- Server-rendered UI: rejected to keep the Python API the single source of
  truth with a decoupled static frontend.
- New `APPROVED` incident status: rejected; the existing
  `waiting_for_approval` pause plus per-recommendation `approved` status and
  the `approvals` row already express the approved state without lifecycle
  churn before remediation exists.
- Executing rollback on approve: rejected; Stage 09 owns remediation.

## Consequences

- New migration `20260906_0006`, `RecommendationStatus` widened to
  `approved/rejected`, frontend Compose service + healthcheck, vitest
  component tests, Python approval contract/integration tests, one Playwright
  spec (route-fulfilled by default, live-capable via env).
- `migrations/env.py` excludes LangGraph checkpoint tables and the HNSW index
  from autogenerate comparison (externally managed); the 0006 downgrade maps
  decided recommendations back to `waiting_for_approval` before restoring the
  narrower constraint.
