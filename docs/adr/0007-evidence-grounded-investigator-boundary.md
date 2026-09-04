# ADR 0007: Evidence-grounded LangGraph investigator boundary

- Status: accepted
- Date: 2026-09-04
- Scope: repository Stage 06 / implementation-plan Stages 4–5

## Context

Stage 05 leaves canonical evidence plus an explicitly empty AI conclusion. Stages 4–5 must turn
that evidence into a schema-valid incident report without giving the model arbitrary query,
execution, or approval powers. Telemetry is untrusted data; provider calls cost money and time;
retried or resumed work must not duplicate artifacts, invent facts, or execute remediation.

## Decision

A typed LangGraph workflow (`packages/agents/workflow.py`) runs eight logical nodes — scope/plan,
collect-or-load, correlate, generate hypotheses, verify, bounded additional collection, sufficiency
validation, report synthesis — with Postgres checkpoint resume per investigation run. The model
reaches telemetry only through two anchor-derived read-only operations (`logs_around_evidence`,
`trace_by_id_from_evidence`); all parameters resolve from incident-owned evidence, never from
model-authored query strings.

The OpenAI Responses API is used exclusively through SDK-native Structured Outputs
(`responses.parse` with Pydantic response models). Provider and model names come from
`Settings`; a `ModelRouter` maps logical operations to configured planning/reasoning models
behind the `StructuredModelProvider` protocol so tests inject deterministic fakes. A
`BudgetedModelGateway` enforces context, call, token, cost, retry, and duration budgets and
persists every attempt as secret-free call metadata.

Deterministic code (`packages/agents/validation.py`) owns all grounding: evidence-ID existence
and incident ownership, closed service/deployment/timestamp vocabularies, support/contradiction
linkage, contradiction rejection, low-confidence caps for unsupported claims, null root cause
with explicit gaps on insufficient evidence, and closed recommendation actions. Rollback is the
only mutating proposal and always requires approval; the worker persists reports but executes
nothing. New tables (`hypotheses`, `incident_reports`, `recommendations`, `investigator_calls`,
`investigation_failures`, LangGraph checkpoints) plus `ai_investigation` run states carry the
durable side; the AI path stays behind `INVESTIGATOR_ENABLED=false` by default.

## Consequences

Reports are reproducible from fixtures, retry/resume-safe by stable IDs, and observable through
model/tool/workflow metrics. Incident API report reads, RAG, frontend approval, and remediation
execution remain later milestones. Live-model verification is a manually invoked,
credential-guarded smoke script, never part of CI.
