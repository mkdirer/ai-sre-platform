# Private GKE cluster: private nodes, no public node IPs, Workload Identity
# for pod-to-GCP auth (no node service-account keys), NetworkPolicy
# enforcement, and GKE-bundled logging/monitoring. The control plane stays
# reachable only from admin_cidrs.

resource "google_container_cluster" "demo" {
  project  = var.project_id
  name     = "${var.resource_name_prefix}-gke"
  location = var.zone

  network    = var.network_name
  subnetwork = var.subnetwork_name

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = true

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.16.0.0/28"
  }

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.admin_cidrs
      content {
        cidr_block   = cidr_blocks.value
        display_name = "admin-${cidr_blocks.key}"
      }
    }
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  network_policy {
    enabled  = true
    provider = "CALICO"
  }

  release_channel {
    channel = "REGULAR"
  }

  maintenance_policy {
    recurring_window {
      start_time = "2026-01-01T02:00:00Z"
      end_time   = "2026-01-01T06:00:00Z"
      recurrence = "FREQ=WEEKLY;BYDAY=SU"
    }
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }
}

resource "google_container_node_pool" "demo" {
  project    = var.project_id
  name       = "${var.resource_name_prefix}-pool"
  location   = var.zone
  cluster    = google_container_cluster.demo.name
  node_count = var.node_count

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  node_config {
    machine_type = var.node_machine_type
    disk_size_gb = 50
    disk_type    = "pd-balanced"

    # Workload Identity is the only pod-to-GCP path: expose the GKE
    # metadata server, never the legacy metadata API.
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring",
      "https://www.googleapis.com/auth/devstorage.read_only",
    ]

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }
}
