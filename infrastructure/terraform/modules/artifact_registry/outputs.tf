output "repository_id" {
  value = google_artifact_registry_repository.demo.repository_id
}

output "registry_host" {
  description = "Registry host for docker push/pull and Helm imageRegistry."
  value       = "${var.region}-docker.pkg.dev"
}
