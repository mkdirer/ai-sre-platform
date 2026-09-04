# Stage 06 — evidence-grounded LangGraph investigator

Implement Stages 4 and 5 from the plan as one carefully bounded AI milestone. Read the security, domain, and eval specifications before editing.

Build a typed LangGraph workflow with checkpointing and these logical nodes:

1. scope/plan the investigation;
2. collect or load metrics, logs, traces, and deployments in parallel;
3. correlate the timeline;
4. generate multiple competing hypotheses;
5. verify each hypothesis against supporting and contradicting evidence;
6. optionally request bounded additional allowlisted evidence;
7. validate evidence sufficiency;
8. generate a final structured IncidentReport and recommendations;
9. stop at `waiting_for_approval` when mutation is recommended.

Use the OpenAI Responses API and Structured Outputs with Pydantic/JSON Schema. Provider/model names come from configuration. Use model routing only behind a clean interface. Tests must use deterministic fake providers and require no API key.

Enforce in deterministic code:

- every evidence ID exists and belongs to the incident;
- no unsupported service/deployment/timestamp/fact enters the final report;
- hypotheses contain support/contradiction IDs;
- unsupported hypotheses have low confidence;
- insufficient evidence yields `root_cause = null` and explicit gaps;
- iteration, tool-call, context, time, retry, and token/cost budgets;
- no remediation action is executed;
- untrusted telemetry cannot issue instructions or select arbitrary tools.

Persist graph/run state, hypotheses, final reports, model/tool call metadata, and failures. Make retry/resume idempotent. Add investigator self-observability: duration, requests, tokens where provided, estimated cost when configured, tool calls/errors, iterations, hypothesis count, and confidence.

System behavior should reflect the investigator rules in the product spec but keep prompts small and composable. Do not embed scenario answers in prompts.

Add agent tests for correct RCA, contradicted hypothesis rejection, missing telemetry, insufficient evidence, malformed model output, nonexistent/cross-incident evidence, prompt injection in logs, loop budget, and API/provider failure. Add an optional manually invoked live-model smoke test guarded by presence of credentials; never run it without explicit local configuration.

Do not add RAG or frontend yet. Run all deterministic quality gates and demonstrate a complete fixture-based investigation report. If running a live test would spend money, ask before doing so. Do not commit or start Stage 07.
