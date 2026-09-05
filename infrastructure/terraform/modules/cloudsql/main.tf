# Private Cloud SQL PostgreSQL with pgvector compatibility, automated
# backups + point-in-time recovery, and no public IP. pgvector arrives via
# the `vector` extension (CREATE EXTENSION vector in the app database after
# migrations run; the flag below makes it available on PG 15+).
# Default version tracks compose (pgvector/pgvector:0.8.0-pg17).

resource "google_sql_database_instance" "demo" {
  project          = var.project_id
  name             = "${var.resource_name_prefix}-pg"
  region           = var.region
  database_version = var.db_version

  deletion_protection = var.deletion_protection

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_autoresize   = true
    disk_size         = 20
    disk_type         = "PD_SSD"

    database_flags {
      name  = "cloudsql.enable_pgvector"
      value = "on"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "03:00"

      backup_retention_settings {
        retained_backups = 7
      }
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = var.network_id

      # Private IP only is not encryption: refuse unencrypted Postgres
      # sessions at the instance level.
      ssl_mode = "ENCRYPTED_ONLY"
    }

    maintenance_window {
      day          = 7
      hour         = 3
      update_track = "stable"
    }

    insights_config {
      query_insights_enabled  = true
      query_string_length     = 1024
      record_application_tags = true
    }
  }
  # No explicit depends_on: the private_network reference already orders
  # creation after the VPC peering.
}

resource "google_sql_database" "demo" {
  project  = var.project_id
  name     = var.db_name
  instance = google_sql_database_instance.demo.name
}

resource "google_sql_user" "demo" {
  project  = var.project_id
  name     = var.db_user
  instance = google_sql_database_instance.demo.name
  password = var.db_password
}
