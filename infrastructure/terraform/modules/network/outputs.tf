output "network_id" {
  value = google_compute_network.demo.id
}

output "network_name" {
  value = google_compute_network.demo.name
}

output "subnetwork_id" {
  value = google_compute_subnetwork.demo.id
}

output "subnetwork_name" {
  value = google_compute_subnetwork.demo.name
}

output "pods_range_name" {
  value = var.pods_cidr_range_name
}

output "services_range_name" {
  value = var.services_cidr_range_name
}

output "private_service_connection" {
  value = google_service_networking_connection.private_services.id
}
