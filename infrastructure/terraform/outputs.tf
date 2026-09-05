output "cluster_name" {
  value = module.gke.cluster_name
}

output "cluster_location" {
  value = module.gke.cluster_location
}

output "cluster_endpoint" {
  value     = module.gke.cluster_endpoint
  sensitive = true
}

output "sql_connection_name" {
  value = module.cloudsql.connection_name
}

output "sql_private_ip" {
  value     = module.cloudsql.private_ip
  sensitive = true
}

output "registry_host" {
  value = module.artifact_registry.registry_host
}

output "deployer_email" {
  value = module.service_accounts.deployer_email
}
