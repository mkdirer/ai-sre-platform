# Versioned image home for CI-pushed app/frontend images. Immutable tags
# and cleanup of untagged pushes keep the registry auditable.

resource "google_artifact_registry_repository" "demo" {
  project       = var.project_id
  location      = var.region
  repository_id = "${var.resource_name_prefix}-images"
  format        = "DOCKER"
  description   = "Versioned ai-sre-platform demo images (CI only)."

  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state = "UNTAGGED"
    }
  }
}
