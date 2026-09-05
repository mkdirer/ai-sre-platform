output "app_email" {
  value = google_service_account.app.email
}

output "worker_email" {
  value = google_service_account.worker.email
}

output "deployer_email" {
  description = "Impersonate via Workload Identity Federation; no keys exist."
  value       = google_service_account.deployer.email
}
