# Agent tests

Deterministic Stage 06 investigator coverage; every test uses scripted fake
providers and in-memory stores, so no `OPENAI_API_KEY` or network is required:

- `test_investigator_workflow.py`: fixture-driven graph runs — correct RCA,
  contradicted-hypothesis rejection, missing-source gaps, insufficient evidence,
  approval pause on rollback, iteration budget, provider outage, unknown/cross
  evidence references, prompt-injected service names, checkpoint resume;
- `test_investigation_validation.py`: grounding rules, confidence policy,
  eligibility ranking, report assembly, recommendation guards, schema rejects;
- `test_model_gateway.py`: retries, context/call/token/cost budgets, model
  routing, usage accounting, no-key configuration failure;
- `test_evidence_tools.py`: anchor ownership, scope enforcement, trace-ID
  resolution, tool-call audit;
- `test_ai_worker_service.py`: report-status mapping, bounded retries,
  idempotent skip, no remediation surface.

Optional live-model smoke: `RUN_LIVE_INVESTIGATOR_SMOKE=true OPENAI_API_KEY=...
uv run python scripts/smoke_investigator_live.py` (or
`make smoke-investigator-live`). It spends real money, never runs in CI, and
refuses without both variables. Fixture demo without credentials:
`make demo-investigation-report`.
