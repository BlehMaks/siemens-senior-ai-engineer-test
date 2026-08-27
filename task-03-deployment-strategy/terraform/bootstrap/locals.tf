locals {
  required_services = toset([
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
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
    var.github_repository_id,
  ) : null

  bootstrap_roles = toset([
    "roles/artifactregistry.admin",
    "roles/cloudtasks.admin",
    "roles/datastore.owner",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/logging.admin",
    "roles/monitoring.notificationChannelEditor",
    "roles/resourcemanager.projectIamAdmin",
    "roles/run.admin",
    "roles/secretmanager.admin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/storage.admin",
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
      project_roles = local.bootstrap_roles
    }
    ci = {
      account_id    = "${var.system_code}-${var.environment}-ci"
      display_name  = "GitHub Actions federation target"
      description   = "Identity impersonated by GitHub Actions through workload identity federation."
      project_roles = []
    }
  }
}
