module "managed_services" {
  source = "../../modules/managed_services"

  project_id                     = var.project_id
  project_number                 = var.project_number
  region                         = var.region
  environment                    = var.environment
  system_code                    = var.system_code
  labels                         = var.labels
  billing_account_id             = var.billing_account_id
  budget_amount_units            = var.budget_amount_units
  budget_alert_thresholds        = var.budget_alert_thresholds
  budget_notification_emails     = var.budget_notification_emails
  api_service_account_email      = var.api_service_account_email
  worker_service_account_email   = var.worker_service_account_email
  deployer_service_account_email = var.deployer_service_account_email
}

module "ingress_policy" {
  source = "../../modules/ingress_policy"

  mode = "hardened"
}

module "model_plane" {
  source = "../../modules/model_plane"

  project_id                     = var.project_id
  region                         = var.region
  environment                    = var.environment
  system_code                    = var.system_code
  model_plane_profile            = "cloud_run_gpu"
  model_image                    = var.model_image
  model_name                     = var.model_name
  worker_service_account_email   = var.worker_service_account_email
  deployer_service_account_email = var.deployer_service_account_email
  model_min_instances            = var.model_min_instances
  model_max_instances            = var.model_max_instances
  model_concurrency              = var.model_concurrency
  gpu_zonal_redundancy_disabled  = var.gpu_zonal_redundancy_disabled
  labels                         = var.labels
}

module "run_services" {
  source = "../../modules/run_services"

  project_id                     = var.project_id
  region                         = var.region
  environment                    = var.environment
  system_code                    = var.system_code
  labels                         = var.labels
  artifact_registry_location     = module.managed_services.artifact_registry.location
  artifact_repository_id         = module.managed_services.artifact_registry.repository_id
  image_digest                   = var.image_digest
  api_service_account_email      = var.api_service_account_email
  worker_service_account_email   = var.worker_service_account_email
  tasks_service_account_email    = var.tasks_service_account_email
  api_ingress                    = module.ingress_policy.policy.api_ingress
  api_default_uri_disabled       = module.ingress_policy.policy.api_default_uri_disabled
  api_allow_unauthenticated      = module.ingress_policy.policy.api_allow_unauthenticated
  worker_ingress                 = module.ingress_policy.policy.worker_ingress
  firestore_database_name        = module.managed_services.firestore.name
  api_key_pepper_secret_id       = var.secret_ids.api_key_pepper
  task_signing_hmac_secret_id    = var.secret_ids.task_signing_hmac
  api_max_instances              = var.api_max_instances
  worker_max_instances           = var.worker_max_instances
  worker_inference_mode          = "ollama"
  model_transport_profile        = "cloud"
  model_base_url                 = module.model_plane.model_plane.service_uri
  model_name                     = module.model_plane.model_plane.model_name
  model_google_id_token_audience = module.model_plane.model_plane.service_uri
  search_backends                = var.search_backends
  action_log_level               = var.action_log_level

  depends_on = [module.managed_services, module.model_plane]
}

output "production_cell" {
  description = "End-to-end production cell with directly wired execution and model planes."
  value = {
    managed_services = {
      firestore         = module.managed_services.firestore
      artifact_registry = module.managed_services.artifact_registry
      logging           = module.managed_services.logging
      budget            = module.managed_services.budget
    }
    ingress_policy = module.ingress_policy.policy
    model_plane    = module.model_plane.model_plane
    api_service    = module.run_services.api_service
    worker_service = module.run_services.worker_service
    dispatch_queue = module.run_services.dispatch_queue
    iam_contract   = module.run_services.iam_contract
  }
}
