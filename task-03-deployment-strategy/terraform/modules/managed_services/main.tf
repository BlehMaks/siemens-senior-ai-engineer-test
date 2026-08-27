locals {
  required_services = toset([
    "artifactregistry.googleapis.com",
    "billingbudgets.googleapis.com",
    "firestore.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "secretmanager.googleapis.com",
  ])

  common_labels = merge(
    var.labels,
    {
      environment = var.environment
      managed_by  = "terraform"
      system      = var.system_code
    },
  )

  api_member    = "serviceAccount:${var.api_service_account_email}"
  worker_member = "serviceAccount:${var.worker_service_account_email}"
  workload_members = toset([
    local.api_member,
    local.worker_member,
  ])

  budget_enabled = (
    var.billing_account_id != "" &&
    var.project_number != "" &&
    var.budget_amount_units > 0 &&
    length(var.budget_notification_emails) > 0
  )
}

resource "google_project_service" "required" {
  for_each = local.required_services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_firestore_database" "assessment" {
  provider = google-beta

  project                           = var.project_id
  name                              = "(default)"
  location_id                       = var.region
  type                              = var.firestore_type
  concurrency_mode                  = "OPTIMISTIC"
  app_engine_integration_mode       = "DISABLED"
  delete_protection_state           = var.firestore_delete_protection_state
  deletion_policy                   = var.firestore_deletion_policy
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "managed" {
  for_each = var.secret_ids

  project   = var.project_id
  secret_id = each.value
  labels    = local.common_labels

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "api_pepper_reader" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.managed["api_key_pepper"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.api_member
}

resource "google_secret_manager_secret_iam_member" "task_hmac_reader" {
  for_each = local.workload_members

  project   = var.project_id
  secret_id = google_secret_manager_secret.managed["task_signing_hmac"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value
}

resource "google_artifact_registry_repository" "containers" {
  provider = google-beta

  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repository_id
  format        = "DOCKER"
  description   = "Reviewed assessment container images."
  labels        = local.common_labels

  docker_config {
    immutable_tags = true
  }

  depends_on = [google_project_service.required]
}

resource "google_artifact_registry_repository_iam_member" "deployer_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.containers.location
  repository = google_artifact_registry_repository.containers.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${var.deployer_service_account_email}"
}

resource "google_project_iam_member" "firestore_user" {
  for_each = local.workload_members

  project = var.project_id
  role    = "roles/datastore.user"
  member  = each.value
}

resource "google_logging_project_bucket_config" "application" {
  project        = var.project_id
  location       = var.region
  bucket_id      = var.log_bucket_id
  retention_days = var.log_retention_days
  description    = "Redacted application logs for the assessment cell."

  depends_on = [google_project_service.required]
}

resource "google_monitoring_notification_channel" "budget_email" {
  for_each = local.budget_enabled ? var.budget_notification_emails : toset([])

  project      = var.project_id
  display_name = "Assessment budget ${each.value}"
  type         = "email"
  labels = {
    email_address = each.value
  }

  depends_on = [google_project_service.required]
}

resource "google_billing_budget" "assessment" {
  count = local.budget_enabled ? 1 : 0

  billing_account = var.billing_account_id
  display_name    = "${var.system_code}-${var.environment}-assessment-budget"

  budget_filter {
    projects = ["projects/${var.project_number}"]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency_code
      units         = tostring(var.budget_amount_units)
    }
  }

  dynamic "threshold_rules" {
    for_each = var.budget_alert_thresholds

    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  all_updates_rule {
    monitoring_notification_channels = [
      for channel in google_monitoring_notification_channel.budget_email :
      channel.name
    ]
    disable_default_iam_recipients = true
  }

  depends_on = [google_project_service.required]
}
