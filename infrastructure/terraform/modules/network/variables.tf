variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "resource_name_prefix" {
  type = string
}

variable "subnet_cidr" {
  description = "Primary subnet range for GKE nodes and pods overflow."
  type        = string
  default     = "10.10.0.0/20"
}

variable "pods_cidr_range_name" {
  type    = string
  default = "pods"
}

variable "pods_cidr" {
  type    = string
  default = "10.11.0.0/16"
}

variable "services_cidr_range_name" {
  type    = string
  default = "services"
}

variable "services_cidr" {
  type    = string
  default = "10.12.0.0/20"
}

variable "private_service_cidr" {
  description = "Range reserved for Private Service Networking (Cloud SQL)."
  type        = string
  default     = "10.20.0.0/20"
}
