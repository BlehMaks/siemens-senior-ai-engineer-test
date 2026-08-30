locals {
  required_services = toset([
    "cloudtasks.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "serviceusage.googleapis.com",
    "sts.googleapis.com",
    "storage.googleapis.com",
  ])

  common_labels = merge(
    var.labels,
    {
      environment = var.environment
      system      = var.system_code
      managed_by  = "terraform"
    },
  )

  github_principal = var.enable_github_wif ? format(
    "principalSet://iam.googleapis.com/%s/attribute.repository_id/%s",
    google_iam_workload_identity_pool.github[0].name,
    data.github_repository.target[0].repo_id,
  ) : null

  github_owner            = split("/", var.github_repository)[0]
  github_repository_name  = split("/", var.github_repository)[1]
  firestore_database_name = "${var.system_code}-${var.environment}"
  worker_service_name     = "${var.system_code}-${var.environment}-worker"
  worker_service_url = format(
    "https://%s-%s.%s.run.app",
    local.worker_service_name,
    data.google_project.current.number,
    var.region,
  )

  github_environment_variables = var.enable_github_wif ? {
    GCP_API_SERVICE_ACCOUNT        = module.identity["api"].email
    GCP_BILLING_ACCOUNT_ID         = var.billing_account_id
    GCP_BUDGET_AMOUNT_UNITS        = tostring(var.budget_amount_units)
    GCP_BUDGET_NOTIFICATION_EMAILS = jsonencode(sort(tolist(var.budget_notification_emails)))
    GCP_DEPLOYER_SERVICE_ACCOUNT   = module.deployer_identity.email
    GCP_PROJECT_ID                 = var.project_id
    GCP_PROJECT_NUMBER             = tostring(data.google_project.current.number)
    GCP_REGION                     = var.region
    GCP_SECRET_IDS                 = jsonencode(var.secret_ids)
    GCP_TASKS_SERVICE_ACCOUNT      = module.identity["tasks"].email
    GCP_TERRAFORM_STATE_BUCKET     = var.application_state_bucket_name
    GCP_WORKER_SERVICE_ACCOUNT     = module.identity["worker"].email
    GCP_WORKLOAD_IDENTITY_PROVIDER = google_iam_workload_identity_pool_provider.github[0].name
  } : {}

  deployer_project_permissions = toset([
    "datastore.databases.create",
    "datastore.databases.getMetadata",
    "datastore.databases.list",
    "datastore.databases.update",
    "datastore.locations.get",
    "datastore.locations.list",
    "datastore.operations.get",
    "datastore.operations.list",
    "resourcemanager.projects.get",
    "run.operations.get",
    "run.services.create",
    "run.services.delete",
    "run.services.get",
    "run.services.update",
  ])

  deployer_project_roles = toset([
    "roles/artifactregistry.admin",
    "roles/logging.configWriter",
    "roles/monitoring.notificationChannelEditor",
    "roles/serviceusage.serviceUsageAdmin",
  ])

  identities = {
    api = {
      account_id    = "${var.system_code}-${var.environment}-api"
      display_name  = "Assessment API runtime"
      description   = "Runtime identity for the assignment API service."
      project_roles = []
    }
    worker = {
      account_id    = "${var.system_code}-${var.environment}-worker"
      display_name  = "Assessment worker runtime"
      description   = "Runtime identity for the assignment worker service."
      project_roles = []
    }
    tasks = {
      account_id    = "${var.system_code}-${var.environment}-tasks"
      display_name  = "Assessment Cloud Tasks caller"
      description   = "OIDC caller identity for Cloud Tasks HTTP delivery."
      project_roles = []
    }
    deployer = {
      account_id    = "${var.system_code}-${var.environment}-deploy"
      display_name  = "Terraform deployer"
      description   = "Identity that applies reviewed Terraform for assignment resources."
      project_roles = local.deployer_project_roles
    }
  }
}
