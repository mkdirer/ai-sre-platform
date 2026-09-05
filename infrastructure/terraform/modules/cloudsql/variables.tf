variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "resource_name_prefix" {
  type = string
}

variable "network_id" {
  description = "VPC id hosting the private service connection."
  type        = string
}

variable "db_version" {
  type = string
}

variable "db_tier" {
  type = string
}

variable "db_name" {
  type = string
}

variable "db_user" {
  type = string
}

variable "db_password" {
  description = "Application password. Sensitive; prefer Secret Manager bootstrap."
  type        = string
  sensitive   = true
}

variable "deletion_protection" {
  description = "Keep true outside deliberate teardowns."
  type        = bool
  default     = true
}
