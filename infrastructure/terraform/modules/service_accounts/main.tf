# Least-privilege identities. Pods authenticate to GCP via Workload
# Identity (KSA -> GSA); CI deploys via Workload Identity Federation with
# no long-lived keys anywhere in this stack.

resource "google_service_account" "app" {
  project      = var.project_id
  account_id   = "${var.resource_name_prefix}-app"
  display_name = "ai-sre demo application workloads"
}

resource "google_service_account" "worker" {
  project      = var.project_id
  account_id   = "${var.resource_name_prefix}-worker"
  display_name = "ai-sre investigator worker"
}

resource "google_service_account" "deployer" {
  project      = var.project_id
  account_id   = "${var.resource_name_prefix}-deployer"
  display_name = "GitHub Actions dev deployer (WIF only)"
}

# App: read logs/metrics + pull images. Nothing else.
resource "google_project_iam_member" "app_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_monitoring" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_registry" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# Worker: app permissions plus Cloud SQL client and per-secret access.
resource "google_project_iam_member" "worker_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_monitoring" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_registry" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_sql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_secrets" {
  for_each = var.secret_ids

  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

# Pods bind to GSAs by Kubernetes service account. Names must equal the
# chart's ServiceAccount names for the release being deployed.
resource "google_service_account_iam_member" "app_workload_identity" {
  service_account_id = google_service_account.app.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.kubernetes_namespace}/${var.app_ksa_name}]"
}

resource "google_service_account_iam_member" "worker_workload_identity" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.kubernetes_namespace}/${var.worker_ksa_name}]"
}

# Deployer: minimal roles to push images, deploy the release, and read state.
resource "google_project_iam_member" "deployer_registry" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_project_iam_member" "deployer_container" {
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# CI impersonates the deployer via WIF. The pool/provider themselves are
# one-time bootstrap (see gcp-bootstrap runbook); this binding consumes the
# exact member string they produced. Empty by default: no repo, fork included,
# receives deploy rights from this stack alone.
resource "google_service_account_iam_member" "deployer_wif" {
  count = var.deployer_wif_member != "" ? 1 : 0

  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = var.deployer_wif_member
}
