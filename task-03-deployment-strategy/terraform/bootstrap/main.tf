resource "google_project_service" "required" {
  for_each = local.required_services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_project_service_identity" "cloud_tasks" {
  provider = google-beta

  project = var.project_id
  service = "cloudtasks.googleapis.com"

  depends_on = [google_project_service.required]
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

resource "google_secret_manager_secret" "managed" {
  for_each = var.secret_ids

  project             = var.project_id
  secret_id           = each.value
  labels              = local.common_labels
  deletion_protection = true

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
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

resource "google_secret_manager_secret_iam_member" "api_pepper_reader" {
  for_each = toset(["api", "worker"])

  project   = var.project_id
  secret_id = google_secret_manager_secret.managed["api_key_pepper"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${module.identity[each.value].email}"
}

resource "google_secret_manager_secret_iam_member" "task_hmac_reader" {
  for_each = toset(["api", "worker"])

  project   = var.project_id
  secret_id = google_secret_manager_secret.managed["task_signing_hmac"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${module.identity[each.value].email}"
}

resource "google_project_iam_custom_role" "deployer_application" {
  project     = var.project_id
  role_id     = "${replace(var.system_code, "-", "_")}_${replace(var.environment, "-", "_")}_terraform_deployer"
  title       = "Assessment Terraform deployer"
  description = "Database, budget, and Cloud Run lifecycle permissions missing from safe predefined roles."
  permissions = local.deployer_project_permissions
  stage       = "GA"

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "deployer_custom_role" {
  project = var.project_id
  role    = google_project_iam_custom_role.deployer_application.name
  member  = "serviceAccount:${module.deployer_identity.email}"
}

resource "google_storage_bucket_iam_member" "deployer_state_objects" {
  bucket = google_storage_bucket.terraform_state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${module.deployer_identity.email}"
}

resource "google_service_account_iam_member" "deployer_runtime_user" {
  for_each = toset(["api", "worker"])

  service_account_id = module.identity[each.value].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${module.deployer_identity.email}"
}

resource "google_service_account_iam_member" "tasks_service_agent_token_creator" {
  service_account_id = module.identity["tasks"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_project_service_identity.cloud_tasks.email}"
}

data "google_cloud_run_v2_service" "worker" {
  count = var.enable_runtime_policy ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = "${var.system_code}-${var.environment}-worker"
}

resource "google_cloud_tasks_queue" "dispatch" {
  count = var.enable_runtime_policy ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = "${var.system_code}-${var.environment}-run-dispatch"

  lifecycle {
    prevent_destroy = true
  }

  rate_limits {
    max_dispatches_per_second = var.queue_max_dispatches_per_second
    max_concurrent_dispatches = var.queue_max_concurrent_dispatches
  }

  retry_config {
    max_attempts       = var.queue_max_attempts
    max_retry_duration = "${var.queue_max_retry_seconds}s"
    min_backoff        = "${var.queue_min_backoff_seconds}s"
    max_backoff        = "${var.queue_max_backoff_seconds}s"
    max_doublings      = 3
  }

  stackdriver_logging_config {
    sampling_ratio = 1
  }

  http_target {
    http_method = "POST"

    uri_override {
      scheme = "HTTPS"
      host   = replace(data.google_cloud_run_v2_service.worker[0].uri, "https://", "")

      path_override {
        path = var.worker_dispatch_path
      }
    }

    oidc_token {
      service_account_email = module.identity["tasks"].email
      audience              = data.google_cloud_run_v2_service.worker[0].uri
    }

    header_overrides {
      header {
        key   = "Content-Type"
        value = "application/json"
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_service_account_iam_member.tasks_service_agent_token_creator,
  ]
}

resource "google_cloud_run_v2_service_iam_binding" "worker_invoker" {
  count = var.enable_runtime_policy ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = data.google_cloud_run_v2_service.worker[0].name
  role     = "roles/run.invoker"
  members  = ["serviceAccount:${module.identity["tasks"].email}"]
}

resource "google_cloud_run_v2_service_iam_member" "api_public_invoker" {
  for_each = var.enable_runtime_policy && var.api_allow_unauthenticated ? toset(["baseline"]) : toset([])

  project  = var.project_id
  location = var.region
  name     = "${var.system_code}-${var.environment}-api"
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_tasks_queue_iam_member" "runtime" {
  for_each = var.enable_runtime_policy ? {
    api_enqueuer = {
      role   = "roles/cloudtasks.enqueuer"
      member = module.identity["api"].email
    }
    api_task_deleter = {
      role   = "roles/cloudtasks.taskDeleter"
      member = module.identity["api"].email
    }
    api_viewer = {
      role   = "roles/cloudtasks.viewer"
      member = module.identity["api"].email
    }
    worker_task_deleter = {
      role   = "roles/cloudtasks.taskDeleter"
      member = module.identity["worker"].email
    }
    worker_viewer = {
      role   = "roles/cloudtasks.viewer"
      member = module.identity["worker"].email
    }
  } : {}

  project  = var.project_id
  location = var.region
  name     = google_cloud_tasks_queue.dispatch[0].name
  role     = each.value.role
  member   = "serviceAccount:${each.value.member}"
}
