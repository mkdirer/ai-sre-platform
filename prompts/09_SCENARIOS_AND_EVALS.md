# Stage 09 — reproducible fault scenarios and evaluation framework

Implement Stage 8 using `docs/EVALS.md` as the contract.

Extend controlled fault injection to support at least:

- slow database;
- database pool exhaustion;
- bad payment deployment/regression;
- inventory upstream timeout;
- CPU saturation with strict local safety limits;
- high application error rate;
- healthy/no actual incident.

Each scenario must be explicit, bounded, reversible, deterministic enough for testing, and automatically cleaned up even after failure. Never run unbounded load or consume host CPU indefinitely. Prefer simulated bounded work where it produces valid telemetry safely.

Create a versioned, machine-readable scenario schema and evaluation runner that:

1. resets/validates the local environment;
2. registers expected deployment state;
3. activates one scenario;
4. generates bounded traffic;
5. waits with deadlines for alert, incident, and investigation;
6. retrieves the structured report;
7. grades normalized root cause, affected service, evidence, unsupported claims, insufficient-evidence decision, recommendation, latency, iterations, calls, and cost metadata;
8. disables the fault and stores diagnostic artifacts;
9. emits JSON and Markdown reports tied to dataset version, Git commit, and model configuration.

Provide a deterministic fake-provider eval suite for CI and an optional live-model suite gated by explicit environment flags and cost budget. Do not run paid evals now without approval. Start with the seven scenarios, then make the framework ready to grow to 20+ without code duplication.

Include missing-source, noisy-signal, unrelated-deployment, ambiguous-evidence, and prompt-injection fixtures. At least two cases must expect `root_cause = null` once the extended dataset is added.

Do not invent performance/accuracy numbers. README may show results only from generated artifacts. Add tests for grader correctness so semantically wrong mechanisms, invented evidence, unsafe recommendations, and null-answer behavior are scored correctly.

Run deterministic scenario/eval tests and applicable gates. Report per-scenario results and limitations. Do not commit or start Stage 10.
