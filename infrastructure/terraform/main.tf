# Root stack: module wiring only. All tuning lives in modules or tfvars;
// no resource is defined here so the blast radius of a change is one module.

module "network" {
  source = "./modules/network"

  project_id           = var.project_id
  region               = var.region
  resource_name_prefix = var.resource_name_prefix
}

module "gke" {
  source = "./modules/gke"

  project_id           = var.project_id
  region               = var.region
  zone                 = var.zone
  resource_name_prefix = var.resource_name_prefix
  network_name         = module.network.network_name
  subnetwork_name      = module.network.subnetwork_name
  pods_range_name      = module.network.pods_range_name
  services_range_name  = module.network.services_range_name
  admin_cidrs          = var.admin_cidrs
}

module "cloudsql" {
  source = "./modules/cloudsql"

  project_id           = var.project_id
  region               = var.region
  resource_name_prefix = var.resource_name_prefix
  network_id           = module.network.network_id
  db_version           = var.db_version
  db_tier              = var.db_tier
  db_name              = var.db_name
  db_user              = var.db_user
  db_password          = var.db_password
}

module "artifact_registry" {
  source = "./modules/artifact_registry"

  project_id           = var.project_id
  region               = var.region
  resource_name_prefix = var.resource_name_prefix
}

module "secret_manager" {
  source = "./modules/secret_manager"

  project_id           = var.project_id
  resource_name_prefix = var.resource_name_prefix
}

module "service_accounts" {
  source = "./modules/service_accounts"

  project_id           = var.project_id
  resource_name_prefix = var.resource_name_prefix
  kubernetes_namespace = var.kubernetes_namespace
  secret_ids           = module.secret_manager.secret_ids
  github_repository    = var.github_repository
  deployer_wif_member  = var.deployer_wif_member
}
