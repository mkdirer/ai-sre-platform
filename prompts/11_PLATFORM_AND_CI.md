# Stage 11 — Kubernetes, Helm, Terraform, and CI/CD

Implement Stage 10 incrementally without applying billable cloud resources. Preserve the fully working Docker Compose demo.

Part A — local Kubernetes and Helm:

- Kubernetes Deployments/Services/ConfigMaps/Secrets references, probes, resources, security contexts, and network exposure appropriate to each component;
- HPA only where metrics and behavior make it meaningful;
- a Helm chart with documented dev values and schema validation;
- migrations handled safely as an explicit job/process;
- local-cluster smoke test instructions and automated manifest rendering/validation.

Part B — Terraform plan for GCP:

- modules/stacks for network, GKE, Cloud SQL PostgreSQL with pgvector compatibility, Artifact Registry, Secret Manager, and required service accounts/permissions;
- remote-state design documented but no hard-coded personal bucket/project;
- variables, outputs, formatting, validation, and static checks;
- least privilege, private connectivity where practical, encryption, backups, and cost-impact documentation;
- do not run `terraform apply`, create a project, enable billable APIs, or modify a cloud account.

Part C — GitHub Actions:

- PR: ruff, format check, mypy, unit/agent tests, frontend lint/type/test/build, Compose/config checks, Terraform/Helm validation, dependency/container/IaC security scans;
- main/manual: build versioned images, generate SBOM/provenance where practical, push using workload identity design, deploy dev with environment protection, run E2E incident test;
- no long-lived credentials in workflow files;
- paid/live-model eval is manual and budget-gated;
- failure artifacts include relevant reports/logs with secret redaction.

Use pinned action versions and image versions. Avoid duplicated configuration and misleading pipeline steps that cannot run. Add tests/validation scripts and update architecture/runbooks.

Validate everything locally that does not require cloud credentials. If a tool is unavailable, document the exact missing prerequisite and do not claim validation. Do not apply cloud changes or commit. Do not start Stage 12.
