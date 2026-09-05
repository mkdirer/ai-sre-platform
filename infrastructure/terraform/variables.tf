variable "project_id" {
  description = "GCP project ID. Never hard-coded; pass via tfvars or environment."
  type        = string

  validation {
    condition     = length(var.project_id) > 0
    error_message = "project_id must be set explicitly (no default project)."
  }
}

variable "region" {
  description = "Primary GCP region for regional resources."
  type        = string
  default     = "europe-west1"
}

variable "zone" {
  description = "Zone for zonal resources (GKE node pool)."
  type        = string
  default     = "europe-west1-b"
}

variable "environment" {
  description = "Deployment environment label (dev only for this demo)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev"], var.environment)
    error_message = "Only the dev environment is defined; extend deliberately."
  }
}

variable "resource_name_prefix" {
  description = "Prefix for resource names to avoid collisions."
  type        = string
  default     = "ai-sre"
}

variable "admin_cidrs" {
  description = "CIDRs allowed to reach the GKE control plane. Must be explicit; there is no open default."
  type        = list(string)

  validation {
    condition     = length(var.admin_cidrs) > 0
    error_message = "admin_cidrs must list at least one admin CIDR (no 0.0.0.0/0 default)."
  }
}

variable "db_tier" {
  description = "Cloud SQL machine tier (smallest demo-viable default)."
  type        = string
  default     = "db-custom-1-3840"
}

variable "db_version" {
  description = "Cloud SQL PostgreSQL version. Must stay on a pgvector-capable major."
  type        = string
  default     = "POSTGRES_17"

  validation {
    condition     = contains(["POSTGRES_15", "POSTGRES_16", "POSTGRES_17"], var.db_version)
    error_message = "db_version must be a pgvector-capable PostgreSQL major (15+)."
  }
}

variable "db_name" {
  description = "Application database name."
  type        = string
  default     = "aisre"
}

variable "db_user" {
  description = "Application database user."
  type        = string
  default     = "aisre"
}

variable "db_password" {
  description = "Application database password for the Cloud SQL login. Sensitive: pass via TF_VAR_db_password at plan time, never commit. Lives in state by provider necessity; see the state-hygiene notes in gcp-bootstrap."
  type        = string
  sensitive   = true
  default     = null
}

variable "github_repository" {
  description = "GitHub repo (org/name) allowed to impersonate the deployer via Workload Identity Federation. Empty disables the binding."
  type        = string
  default     = ""
}

variable "deployer_wif_member" {
  description = "Full WIF principalSet for the deployer binding (from the bootstrap runbook). Empty disables it."
  type        = string
  default     = ""
}

variable "kubernetes_namespace" {
  description = "Namespace the demo release occupies (must match Helm --namespace)."
  type        = string
  default     = "ai-sre-demo"
}
