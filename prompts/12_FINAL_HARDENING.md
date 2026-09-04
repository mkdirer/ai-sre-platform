# Stage 12 — final hardening and portfolio delivery

Do not add broad new features. Treat this as release hardening for a 3–5 minute portfolio demo.

Read every product, architecture, security, quality, and eval document. Inspect the full repository, Git history/diff, dependency locks, migrations, Compose/Helm/Terraform assets, CI, tests, and README. Produce a prioritized gap list before editing. Fix release-blocking and high-value gaps within existing scope.

Required final outcomes:

- `make demo` or one equally simple documented command starts the local platform from a clean state;
- readiness polling replaces brittle fixed sleeps;
- one scenario command demonstrates bad payment deployment through detection, investigation, hypotheses, verification, recommendation, approval, rollback, recovery, and resolution;
- the demo can be reset and rerun without manual database surgery;
- dashboards and frontend clearly show a healthy baseline, degradation, timeline, evidence, rejected hypotheses, RCA/confidence or gaps, approval, and recovery;
- architecture, threat model, operational runbooks, troubleshooting, ADRs, API, and data model match the code;
- eval results are reproducible and all published numbers come from stored reports;
- a clean-clone quickstart has been executed exactly as written;
- secrets and generated/local artifacts are absent from Git;
- license/attribution and dependency/container vulnerabilities are reviewed;
- CI represents real runnable commands;
- resume-ready project description contains no unmeasured claims.

Run `/review`-equivalent full review with emphasis on correctness, concurrency/idempotency, migrations, evidence invariants, prompt injection, approval/remediation safety, test validity, observability cardinality, and demo reproducibility. Add regression tests for every fixed defect.

Execute the deterministic full quality gate and run the complete demo twice from a clean state. Do not spend money, invoke paid live-model evals, deploy cloud resources, publish repositories/images, or create a release unless explicitly requested.

Finish with:

1. release-readiness verdict;
2. exact test/demo commands and results;
3. remaining risks and honest limitations;
4. measured eval summary;
5. concise architecture/security tradeoffs suitable for interview discussion;
6. recommended manual actions before publishing.

Do not commit automatically.
