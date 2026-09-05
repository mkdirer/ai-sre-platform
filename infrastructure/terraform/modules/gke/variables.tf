variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "zone" {
  type = string
}

variable "resource_name_prefix" {
  type = string
}

variable "network_name" {
  type = string
}

variable "subnetwork_name" {
  type = string
}

variable "pods_range_name" {
  type = string
}

variable "services_range_name" {
  type = string
}

variable "admin_cidrs" {
  description = "CIDRs allowed to reach the control plane (no open default)."
  type        = list(string)
}

variable "node_machine_type" {
  description = "Node machine type (demo-sized default)."
  type        = string
  default     = "e2-standard-2"
}

variable "node_count" {
  type    = number
  default = 2
}
