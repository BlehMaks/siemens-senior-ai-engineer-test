locals {
  required_services = toset([
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
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
    var.github_repository_id,
  ) : null

  deployer_project_permissions = toset([
    "billing.resourcebudgets.read",
    "billing.resourcebudgets.write",
    "datastore.databases.create",
    "datastore.databases.delete",
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
    "run.services.getIamPolicy",
    "run.services.setIamPolicy",
    "run.services.update",
  ])

  deployer_project_roles = toset([
    "roles/artifactregistry.admin",
    "roles/cloudtasks.queueAdmin",
    "roles/datastore.indexAdmin",
    "roles/logging.configWriter",
    "roles/monitoring.notificationChannelEditor",
    "roles/serviceusage.serviceUsageAdmin",
  ])

  tasks_policy_permissions = toset([
    "iam.serviceAccounts.get",
    "iam.serviceAccounts.getIamPolicy",
    "iam.serviceAccounts.setIamPolicy",
  ])

  identities = {
    api = {
      account_id    = "${var.system_code}-${var.environment}-api"
      display_name  = "Assessment API runtime"
      description   = "Runtime identity for the assignment API service."
      project_roles = ["roles/datastore.user"]
    }
    worker = {
      account_id    = "${var.system_code}-${var.environment}-worker"
      display_name  = "Assessment worker runtime"
      description   = "Runtime identity for the assignment worker service."
      project_roles = ["roles/datastore.user"]
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
    ci = {
      account_id    = "${var.system_code}-${var.environment}-ci"
      display_name  = "GitHub Actions federation target"
      description   = "Identity impersonated by GitHub Actions through workload identity federation."
      project_roles = []
    }
  }
}
