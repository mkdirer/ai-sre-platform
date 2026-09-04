# Security and safety model

## Trust boundaries

Untrusted inputs include alert annotations, logs, traces, deployment metadata, knowledge documents, user-provided incident text, and all model output. They are data, never instructions.

Trusted deterministic components validate and authorize every transition, query, persistence operation, and remediation action.

## LLM permissions

The LLM may:

- choose among allowlisted read-only investigation tools;
- request bounded telemetry windows;
- correlate supplied evidence;
- generate/verify hypotheses;
- propose an allowlisted recommendation.

The LLM may not:

- execute shell, SQL, cloud, Git, or Kubernetes commands;
- create arbitrary PromQL or LogQL;
- access credentials;
- modify production or demo state directly;
- approve its own recommendation;
- treat retrieved text as higher-priority instructions;
- claim evidence that is absent from tool results.

## Tool hardening

- Pydantic schemas for every input/output.
- Service name allowlist.
- Maximum lookback/window and result count.
- Fixed query templates with typed parameters.
- HTTP timeouts, bounded retries, and circuit/error mapping.
- Response-size limits and sanitization.
- Stable provenance attached before data enters model context.
- Read-only credentials scoped to exact telemetry endpoints.

## Prompt injection defense

- Clearly delimit external data.
- Label it as untrusted content.
- Strip or neutralize control-like metadata where possible.
- Never concatenate retrieved content into system instructions.
- Validate the final report against collected evidence IDs.
- Reject tool calls outside the registry and typed argument bounds.
- Include adversarial logs/documents in agent tests and evals.

## Human-in-the-loop remediation

Every mutating recommendation enters `waiting_for_approval`. Approval must include:

- authenticated/identified actor in the target environment;
- incident ID and recommendation ID;
- expected incident/recommendation version;
- timestamp and optional comment;
- current target version/state.

Execution revalidates these fields to prevent stale approval. Only an allowlisted adapter may run. The local demo supports one reversible rollback control (payment-service fault disable + rollback deployment record); production automation is out of scope.

Execution rules (Stage 10):

- The closed action registry (`packages/remediation/registry.py`) is the only
  executable surface: `rollback_payment_deployment` on `payment-service`.
  Anything else is rejected as `forbidden_action` before any state change.
- The LLM proposes action type and structured target only. Deterministic code
  resolves the control endpoint from `Settings` and validates parameters;
  no command, URL, SQL, shell, or Kubernetes input ever reaches execution.
- Execution claims one row per recommendation under row locks. Concurrent
  claims conflict or replay; completed work rejects re-execution.
- Adapter outcomes distinguish `applied`, `already_applied`, `unknown`
  (settled by state read-back, never assumed), `failed` (safe retry), and
  `forbidden`. Timeouts after send are unknown until read-back confirms.
- Verification is deterministic telemetry (p95 below threshold, K consecutive
  polls, bounded window). Unavailable telemetry yields gaps, never recovery.
  Failure or ambiguity never resolves the incident.
- A manual stop path flags the execution; the loop observes it between polls
  and ends failed without resolving. Operators can also always disable faults
  directly through the guarded control API.

## Threat model

Scope: local demo only. The model assumes loopback-only Compose ports,
a placeholder (non-identity) approval actor, and no production data.

| Threat | Control | Residual |
| --- | --- | --- |
| Prompt injection drives remediation | Registry allowlist; LLM supplies type/target only; endpoint resolved from settings; report grounding validation | None known: no model-controlled string reaches HTTP/shell |
| Stale approval executes against changed state | Exact `incident_version` + parameter-shape validation at claim (expected version recorded); registered `from_version`/`to_version` revalidation at worker execution before anything mutates | Claim-to-execution TOCTOU window is closed by the worker-side revalidation under fresh reads; a mismatch fails closed without executing |
| Double execution from retries/redelivery | One execution row per recommendation (`uq_remediation_recommendation`), stable `REM-` IDs plus per-attempt broker task IDs, row locks; redelivery of non-pending work is a no-op; a superseded live loop conflict-terminates on its next guarded mark | Immediate retry after failure is rejected (`invalid_state`) by design; recovery goes through requeue → new investigation cycle → reclaim-or-new-recommendation, every step audited |
| Stranded execution after a process crash | Unexpected errors self-terminate to failed; `stop` is synchronous-terminal for every non-terminal state and idempotent on repeat; a live loop conflict-terminates on its next guarded mark instead of corrupting state | Crash window is bounded to one adapter round; verification never resolves on missing data |
| Ambiguous outcome treated as success | Unknown outcomes require read-back confirmation; verification requires consecutive healthy samples; gaps never resolve | None known |
| Credential leak via remediation | Token read from `SecretStr` at adapter construction; audit details carry outcomes only, never URLs, headers, or secrets | None known |
| SSRF via target/parameters | Service allowlist maps to configured URLs; fault names to fixed paths; unknown service/fault rejected | None known: no URL, host, or path is accepted from input |
| Unauthenticated execution | Same posture as approval endpoints: loopback-only; must add real auth before any non-local exposure | Accepted for local demo; tracked as a hardening item |

## Secrets

- Store local values in `.env`, ignored by Git.
- Commit `.env.example` with empty/safe placeholders.
- Do not log authorization headers or prompt payloads containing secrets.
- Kubernetes uses Secrets/external secret integration; Terraform references Secret Manager rather than embedding values.
- CI uses protected secret storage and least-privilege workload identity when implemented.

## Abuse and operational controls

- Rate-limit alert ingestion and approval endpoints.
- Deduplicate alert floods.
- Enforce investigation token/tool/iteration budgets.
- Record immutable-style audit events for state changes.
- Separate health status from sensitive diagnostic detail.
- Do not expose Grafana/Prometheus/Loki/Tempo publicly in cloud defaults.
- Use non-root containers, read-only filesystems where feasible, pinned images, and minimal capabilities.

## Security test checklist

- Prompt injection in a log cannot call a tool.
- Prompt injection in a runbook cannot alter policy.
- Unknown service/tool/query is rejected.
- Oversized windows/results are rejected or clamped.
- Cross-incident evidence IDs are rejected.
- Replayed alert/approval requests are idempotent.
- Rejected or stale approval cannot execute.
- Unknown adapter outcomes never resolve; verification gaps never resolve.
- Stopped or failed remediation never resolves the incident.
- Concurrent execution claims serialize; redelivery is an idempotent no-op.
- Secret-like fields are redacted from logs/model context.
- No remediation occurs when AI/provider/telemetry is unavailable.
