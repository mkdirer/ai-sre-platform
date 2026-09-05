output "cluster_name" {
  value = google_container_cluster.demo.name
}

output "cluster_location" {
  value = google_container_cluster.demo.location
}

output "cluster_endpoint" {
  description = "Control-plane endpoint (authorized networks only)."
  value       = google_container_cluster.demo.endpoint
  sensitive   = true
}

output "cluster_ca_certificate" {
  value     = google_container_cluster.demo.master_auth[0].cluster_ca_certificate
  sensitive = true
}
