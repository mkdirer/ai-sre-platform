# Stage 10 — approved remediation and deterministic recovery verification

Implement only Stage 9. The system may execute one or more tightly allowlisted local demo actions after explicit approval. It must never expose arbitrary shell, SQL, HTTP targets, or Kubernetes commands to the LLM.

Implement an action registry with typed schemas. Start with reversible rollback of the demo payment service from a registered bad version/fault state to its known previous version/state. Recommendation parameters are generated/validated separately from execution.

Execution requirements:

- only approved recommendation ID and exact current version/state may execute;
- stale, rejected, replayed, mismatched, or already-completed actions are safely rejected/idempotent;
- authorization/actor placeholder, approval, execution attempt, result, and verification are audited;
- bounded timeout/retry rules distinguish unknown outcome from safe retry;
- failure never marks the incident resolved;
- graph resumes from approval, executes via the allowlisted adapter, and enters `verifying`;
- deterministic telemetry thresholds and bounded observation window verify recovery;
- successful recovery changes status to `resolved`; ambiguous recovery stays unresolved with gaps;
- a manual stop/disable path exists.

The LLM may propose the action type and structured target, but deterministic code resolves the actual local endpoint/action and validates it. It cannot supply a command or URL.

Add unit/integration/E2E tests covering approval → rollback → telemetry recovery → resolved, plus stale approval, concurrent execution, partial/unknown failure, no recovery, provider outage, replay, and forbidden action. Update security docs and threat model.

Do not add production auto-remediation or cloud mutation. Run the complete local 3–5 minute scenario twice from a clean state and report measured timing and any flakiness. Do not commit or start Stage 11.
