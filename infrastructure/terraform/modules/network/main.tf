# Least-privilege demo network: custom VPC, private subnets with secondary
# ranges for GKE pods/services, Private Google Access for Google APIs, Cloud
# NAT for controlled egress, and Private Service Networking for Cloud SQL.

resource "google_compute_network" "demo" {
  project                 = var.project_id
  name                    = "${var.resource_name_prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "demo" {
  project                  = var.project_id
  name                     = "${var.resource_name_prefix}-subnet"
  region                   = var.region
  network                  = google_compute_network.demo.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = var.pods_cidr_range_name
    ip_cidr_range = var.pods_cidr
  }

  secondary_ip_range {
    range_name    = var.services_cidr_range_name
    ip_cidr_range = var.services_cidr
  }

  log_config {
    aggregation_interval = "INTERVAL_5_MIN"
    flow_sampling        = 0.1
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_router" "demo" {
  project = var.project_id
  name    = "${var.resource_name_prefix}-router"
  region  = var.region
  network = google_compute_network.demo.id
}

resource "google_compute_router_nat" "demo" {
  project                            = var.project_id
  name                               = "${var.resource_name_prefix}-nat"
  region                             = var.region
  router                             = google_compute_router.demo.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.demo.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

resource "google_compute_global_address" "private_services" {
  project       = var.project_id
  name          = "${var.resource_name_prefix}-psn"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  address       = split("/", var.private_service_cidr)[0]
  prefix_length = tonumber(split("/", var.private_service_cidr)[1])
  network       = google_compute_network.demo.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.demo.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
}
