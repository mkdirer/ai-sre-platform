output "secret_ids" {
  description = "Full secret IDs for least-privilege IAM bindings."
  value       = { for name, secret in google_secret_manager_secret.demo : name => secret.id }
}
