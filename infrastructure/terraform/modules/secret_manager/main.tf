# Secret shells always exist; versions are created OUTSIDE Terraform by
# the bootstrap runbook (`gcloud secrets versions add`). Secret values
# never enter variables, plans, or state:
#
#   printf '%s' "$FAULT_CONTROL_TOKEN" | gcloud secrets versions add \
#     ai-sre-fault-control-token --data-file=-
#
# The database password is the one exception: Cloud SQL login requires it
# at apply time (see cloudsql module), so the state bucket must keep
# uniform bucket-level access + versioning per the bootstrap runbook.

resource "google_secret_manager_secret" "demo" {
  for_each = toset(["db-password", "fault-control-token", "openai-api-key"])

  project   = var.project_id
  secret_id = "${var.resource_name_prefix}-${each.key}"

  replication {
    auto {}
  }
}
