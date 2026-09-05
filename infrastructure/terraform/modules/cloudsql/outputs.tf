output "instance_name" {
  value = google_sql_database_instance.demo.name
}

output "connection_name" {
  description = "Instance connection name for the Cloud SQL Auth Proxy / connector."
  value       = google_sql_database_instance.demo.connection_name
}

output "private_ip" {
  value     = google_sql_database_instance.demo.private_ip_address
  sensitive = true
}

output "database_name" {
  value = google_sql_database.demo.name
}
