variable "project_id" {
  type = string
}

variable "resource_name_prefix" {
  type = string
}

variable "kubernetes_namespace" {
  type = string
}

variable "app_ksa_name" {
  description = "Chart ServiceAccount for app pods (<release>-ai-sre-platform-app). Must match helm install --name used by deploy."
  type        = string
  default     = "demo-ai-sre-platform-app"
}

variable "worker_ksa_name" {
  description = "Chart ServiceAccount for the worker (<release>-ai-sre-platform-worker)."
  type        = string
  default     = "demo-ai-sre-platform-worker"
}

variable "secret_ids" {
  description = "Secret IDs the worker may read (least privilege per secret)."
  type        = map(string)
}

variable "github_repository" {
  description = "Informational only: org/name this stack was planned for. Grants never derive from it."
  type        = string
  default     = ""
}

variable "deployer_wif_member" {
  description = "Full WIF principalSet allowed to impersonate the deployer (from the bootstrap runbook). Empty disables the binding."
  type        = string
  default     = ""
}
