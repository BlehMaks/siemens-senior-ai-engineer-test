resource "google_project_service" "required" {
  for_each = local.required_services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "terraform_state" {
  name                        = var.state_bucket_name
  location                    = var.region
  project                     = var.project_id
  storage_class               = "STANDARD"
  labels                      = local.common_labels
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  depends_on = [google_project_service.required]
}

resource "google_iam_workload_identity_pool" "github" {
  count = var.enable_github_wif ? 1 : 0

  project                   = var.project_id
  workload_identity_pool_id = "${var.system_code}-${var.environment}-github"
  display_name              = "GitHub Actions federation"
  description               = "Short-lived GitHub OIDC trust for reviewed Terraform runs."

  depends_on = [google_project_service.required]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = var.enable_github_wif ? 1 : 0

  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "GitHub Actions provider"
  description                        = "Repository-scoped GitHub OIDC provider."
  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.actor"         = "assertion.actor"
    "attribute.environment"   = "assertion.environment"
    "attribute.ref"           = "assertion.ref"
    "attribute.repository"    = "assertion.repository"
    "attribute.repository_id" = "assertion.repository_id"
  }
  attribute_condition = join(" && ", compact([
    "attribute.repository_id == \"${var.github_repository_id}\"",
    "attribute.repository == \"${var.github_repository}\"",
    "attribute.ref == \"refs/heads/${var.github_branch}\"",
    var.github_environment == "" ? "" : "attribute.environment == \"${var.github_environment}\"",
  ]))

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

module "identity" {
  for_each = {
    for name, identity in local.identities :
    name => identity if name != "deployer"
  }

  source        = "../modules/identity"
  project_id    = var.project_id
  account_id    = each.value.account_id
  display_name  = each.value.display_name
  description   = each.value.description
  labels        = local.common_labels
  project_roles = each.value.project_roles
  workload_identity_members = (
    each.key == "ci" && local.github_principal != null
    ? { github = local.github_principal }
    : {}
  )
  service_account_user_members = {}
  token_creator_members        = {}

  depends_on = [google_project_service.required]
}

module "deployer_identity" {
  source        = "../modules/identity"
  project_id    = var.project_id
  account_id    = local.identities.deployer.account_id
  display_name  = local.identities.deployer.display_name
  description   = local.identities.deployer.description
  labels        = local.common_labels
  project_roles = local.identities.deployer.project_roles
  service_account_user_members = {
    ci = "serviceAccount:${module.identity["ci"].email}"
  }
  token_creator_members = {
    ci = "serviceAccount:${module.identity["ci"].email}"
  }

  depends_on = [google_project_service.required]
}

resource "google_service_account_iam_member" "deployer_runtime_user" {
  for_each = toset(["api", "tasks", "worker"])

  service_account_id = module.identity[each.value].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${module.deployer_identity.email}"
}
