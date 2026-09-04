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

Execution revalidates these fields to prevent stale approval. Only an allowlisted adapter may run. The local demo initially supports one reversible rollback control; production automation is out of scope.

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
- Secret-like fields are redacted from logs/model context.
- No remediation occurs when AI/provider/telemetry is unavailable.
