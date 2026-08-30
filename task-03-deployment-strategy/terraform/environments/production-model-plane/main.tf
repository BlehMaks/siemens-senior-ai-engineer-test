module "model_plane" {
  source = "../../modules/model_plane"

  project_id                     = var.project_id
  region                         = var.region
  environment                    = var.environment
  system_code                    = var.system_code
  model_plane_profile            = var.model_plane_profile
  model_image                    = var.model_image
  model_name                     = var.model_name
  worker_service_account_email   = var.worker_service_account_email
  deployer_service_account_email = var.deployer_service_account_email
  model_min_instances            = var.model_min_instances
  model_max_instances            = var.model_max_instances
  gpu_zonal_redundancy_disabled  = var.gpu_zonal_redundancy_disabled
  labels                         = var.labels
}

output "model_plane" {
  description = "Production model-plane deployment contract."
  value       = module.model_plane.model_plane
}
