module "managed_services" {
  source = "../../modules/managed_services"

  project_id                     = var.project_id
  project_number                 = var.project_number
  region                         = var.region
  environment                    = "dev"
  system_code                    = var.system_code
  labels                         = var.labels
  billing_account_id             = var.billing_account_id
  budget_notification_emails     = var.budget_notification_emails
  api_service_account_email      = var.api_service_account_email
  worker_service_account_email   = var.worker_service_account_email
  deployer_service_account_email = var.deployer_service_account_email
}

module "ingress_policy" {
  source = "../../modules/ingress_policy"

  mode = var.ingress_mode
}

module "run_services" {
  source = "../../modules/run_services"

  project_id                   = var.project_id
  region                       = var.region
  environment                  = "dev"
  system_code                  = var.system_code
  labels                       = var.labels
  artifact_registry_location   = module.managed_services.artifact_registry.location
  artifact_repository_id       = module.managed_services.artifact_registry.repository_id
  image_digest                 = var.image_digest
  api_service_account_email    = var.api_service_account_email
  worker_service_account_email = var.worker_service_account_email
  tasks_service_account_email  = var.tasks_service_account_email
  api_ingress                  = module.ingress_policy.policy.api_ingress
  api_default_uri_disabled     = module.ingress_policy.policy.api_default_uri_disabled
  api_allow_unauthenticated    = module.ingress_policy.policy.api_allow_unauthenticated
  worker_ingress               = module.ingress_policy.policy.worker_ingress
  firestore_database_name      = module.managed_services.firestore.name
  api_key_pepper_secret_id     = module.managed_services.secret_containers.api_key_pepper
  task_signing_hmac_secret_id  = module.managed_services.secret_containers.task_signing_hmac

  depends_on = [module.managed_services]
}

output "managed_services" {
  description = "Thin wrapper around the reusable C04 managed-services module."
  value = {
    firestore         = module.managed_services.firestore
    secret_containers = module.managed_services.secret_containers
    artifact_registry = module.managed_services.artifact_registry
    logging           = module.managed_services.logging
    budget            = module.managed_services.budget
    workload_access   = module.managed_services.workload_access
  }
}

output "execution_plane" {
  description = "C05A execution-plane and ingress contracts."
  value = {
    ingress_policy = module.ingress_policy.policy
    api_service    = module.run_services.api_service
    worker_service = module.run_services.worker_service
    dispatch_queue = module.run_services.dispatch_queue
    iam_contract   = module.run_services.iam_contract
  }
}
