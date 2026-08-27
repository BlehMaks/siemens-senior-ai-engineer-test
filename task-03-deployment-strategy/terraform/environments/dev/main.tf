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
