# ADR 0010: Bounded multi-fault eval scenarios and deterministic grading

- Status: Accepted
- Date: 2026-09-04
- Scope: Stage 09 / implementation-plan Stage 8 fault scenarios and evals

## Context

Stage 08 left one `slow_database` fault, one live scenario script, and no
machine-readable eval dataset. `docs/EVALS.md` requires seven initial
fault/healthy scenarios growing to 20+, with missing-source, noisy,
unrelated-deployment, ambiguous, and injection fixtures, plus JSON/Markdown
reports tied to dataset version, commit, and model config. Live faults must
stay explicit, bounded, reversible, and auto-cleaned; CPU work must never burn
the host.

## Decision

Extend the allowlisted fault boundary without creating a general executor:

- `FaultName` enum adds `pool_exhaustion`, `bad_deployment`,
  `inventory_timeout`, `cpu_saturation`, `high_error_rate` alongside
  `slow_database`. Payment owns five faults; inventory owns one timeout.
- The legacy `SlowDatabaseFaultController` and its
  `/internal/faults/slow-database` routes are preserved byte-for-byte for
  contract compatibility. New faults live in a shared
  `apps/demo/common/faults.py` `MultiFaultController` with the same
  environment + opt-in + constant-time token guard, lock-protected state,
  always-off init, and per-fault bounded delays from validated settings.
- Effects are simulated: fixed sleeps (pool 1.0s, bad-deploy 1.2s, inventory
  1.5s, CPU 0.2s plus a 2k-iteration hash loop, no background burner) and a
  deterministic hash-based 50% error marker for `high_error_rate`. Generic
  `/internal/faults/{name}` routes accept kebab or snake names, reject
  cross-service and unknown faults with 404, and list via
  `/internal/faults`. `demo_fault_enabled{service,fault}` gains the five new
  labels through `set_fault_enabled`; the old setter delegates to it.
- Eval cases are versioned JSON under `evals/scenarios/*.json`
  (`schema_version 1.0`, `dataset_version v1` for SCN-001..007,
  `v1-extended` for SCN-008..012). The loader discovers files without
  hardcoding IDs, so SCN-013+ needs no code change. `v1-extended` is a
  superset of `v1` (12 total, three nulls: healthy, ambiguous,
  missing-source).
- `packages/evals/` owns the schema, deterministic grader, offline fixtures,
  runners, and artifact writers. The grader implements EVALS rules with
  normalized labels, invented-evidence failure, wrong-mechanism failure,
  coincidental-deployment discipline, correct-null rewards, approval-gated
  recommendation safety, and budget checks. Offline fixtures run the real
  LangGraph workflow with `ScriptedProvider` and synthetic canonical
  evidence; no network or credentials are required.
- `scripts/run_evals.py` runs `--dataset v1` (CI regression gate),
  `--dataset v1-extended` (12, full), or `--mode live` (best-effort fault
  reset, bounded fault + traffic + deployments, deadline-bounded incident and
  report waits with deterministic grading when a report is retrieved,
  guaranteed cleanup, diagnostic `eval-live-<dataset>.json`). Live mode requires
  `RUN_LIVE_EVALS=1`, `EVAL_LIVE_CONFIRM=1`, and `EVAL_MAX_COST_USD>0`, and
  never runs paid models without those flags. Artifacts land in
  `evals/results/eval-<dataset>.{json,md}` with dataset, commit, model,
  timestamp, per-scenario grades, and aggregate metrics.

## Consequences

- restarting a demo service clears its faults safely;
- CPU/error faults are visibly simulated rather than host-loading;
- eval growth is data-only until new mechanisms need new templates;
- live coverage beyond slow_database variants still needs per-fault
  Prometheus alerts for full alert→incident→report grading; scenarios
  without `expect_alert` skip the incident/report wait and record why, so
  pool/bad-deploy/inventory/cpu/error faults are traffic-and-cleanup
  checked live and fully graded offline;
- the pre-existing `validation._category_supported` `"value": 0` substring
  check falsely matches values like `0.48`; eval fixtures avoid the `value`
  key rather than changing validation in this stage.
