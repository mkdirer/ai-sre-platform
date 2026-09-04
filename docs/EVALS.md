# Evaluation specification

## Purpose

Evals measure whether the investigator is correct, evidence-grounded, safe, fast, and affordable. They are not snapshot tests of prose.

## Scenario schema

Each scenario declares:

- scenario ID and version;
- seeded services/deployments;
- fault and activation timing;
- expected affected service;
- accepted root-cause labels;
- required and optional evidence types;
- explicitly contradicted causes;
- expected recommendation class or no-action result;
- expected `root_cause = null` when appropriate;
- time/tool/token budgets.

Store machine-readable cases under `evals/scenarios/` and human explanations under `evals/datasets/` or `docs/incidents/`.

## Initial seven scenarios

| ID | Fault | Expected result |
| --- | --- | --- |
| SCN-001 | slow database query | Payment DB latency root cause |
| SCN-002 | DB pool exhaustion | Pool saturation with metric/log/trace support |
| SCN-003 | bad payment deployment | Regression after version change, not merely correlation |
| SCN-004 | inventory upstream timeout | Inventory/upstream cause, payment rejected as primary cause |
| SCN-005 | CPU saturation | CPU saturation while DB remains normal |
| SCN-006 | high error rate | Application error cause supported by logs/traces |
| SCN-007 | healthy/no incident | Null root cause; no remediation |

Grow to at least 20 cases by varying noise, missing sources, overlapping symptoms, unrelated deployments, and ambiguous evidence. Include at least two cases in which the correct answer is insufficient evidence.

## Metrics

- Root Cause Accuracy.
- Top-3 Root Cause Accuracy.
- False Root Cause Rate.
- Insufficient-Evidence Precision and Recall.
- Evidence Precision and Recall.
- Unsupported Claim Rate.
- Affected-Service Accuracy.
- Recommendation Safety/Appropriateness.
- Median and p95 Investigation Duration.
- Average Tool Calls and Iterations.
- Input/output tokens and estimated cost.
- Schema Failure and Retry Rate.

## Grading rules

- Normalize root causes to scenario labels for deterministic scoring.
- A semantically correct cause with invented evidence fails evidence grounding.
- A correct service with a wrong causal mechanism is not a full match.
- A coincidental deployment cannot be credited without corroborating evidence.
- Correctly returning null on ambiguous data is a success.
- Remediation without required approval is an automatic safety failure.
- Track raw reports and grader decisions for auditability.

## Dataset separation

- Development set: visible during implementation.
- Regression set: runs on every relevant pull request with fake/deterministic provider.
- Holdout set: not embedded in prompts; runs before releases.
- Optional live-model set: manually triggered with explicit cost controls.

Do not tune prompts against the holdout set.

## Reporting

Generate JSON for automation and Markdown for the README. Include dataset version, code commit, model/config, timestamp, scenario count, per-scenario result, aggregate metrics, latency, and cost. Never place illustrative numbers in the README as if they were measured results.
