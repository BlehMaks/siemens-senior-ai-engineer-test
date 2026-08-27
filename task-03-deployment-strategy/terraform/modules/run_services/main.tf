locals {
  required_services = toset([
    "cloudtasks.googleapis.com",
    "run.googleapis.com",
  ])

  common_labels = merge(
    var.labels,
    {
      environment = var.environment
      managed_by  = "terraform"
      system      = var.system_code
    },
  )

  tasks_service_agent_member = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"

  image = format(
    "%s-docker.pkg.dev/%s/%s/%s@%s",
    var.artifact_registry_location,
    var.project_id,
    var.artifact_repository_id,
    var.image_name,
    var.image_digest,
  )

  dispatch_queue_name = "${var.system_code}-${var.environment}-run-dispatch"
  dispatch_queue_path = "projects/${var.project_id}/locations/${var.region}/queues/${local.dispatch_queue_name}"

  api_plain_env = {
    AGENT_API_SERVICE_ROLE        = "api"
    AGENT_API_INFERENCE_MODE      = "disabled"
    AGENT_API_SHUTDOWN_SECONDS    = tostring(var.shutdown_seconds)
    AGENT_API_GCP_PROJECT_ID      = var.project_id
    AGENT_API_FIRESTORE_DATABASE  = var.firestore_database_name
    AGENT_API_CLOUD_TASKS_QUEUE   = local.dispatch_queue_path
    AGENT_API_TASK_TARGET_URL     = "${google_cloud_run_v2_service.worker.uri}${var.worker_dispatch_path}"
    AGENT_API_QUEUE_DELIVERY_PATH = var.worker_dispatch_path
  }

  worker_plain_env = {
    AGENT_API_SERVICE_ROLE        = "worker"
    AGENT_API_INFERENCE_MODE      = "fake"
    AGENT_API_SHUTDOWN_SECONDS    = tostring(var.shutdown_seconds)
    AGENT_API_GCP_PROJECT_ID      = var.project_id
    AGENT_API_FIRESTORE_DATABASE  = var.firestore_database_name
    AGENT_API_CLOUD_TASKS_QUEUE   = local.dispatch_queue_path
    AGENT_API_QUEUE_DELIVERY_PATH = var.worker_dispatch_path
  }
}

data "google_project" "current" {
  project_id = var.project_id
}

resource "google_project_service" "required" {
  for_each = local.required_services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_cloud_run_v2_service" "api" {
  provider = google-beta

  project              = var.project_id
  location             = var.region
  name                 = "${var.system_code}-${var.environment}-api"
  ingress              = var.api_ingress
  launch_stage         = "BETA"
  deletion_protection  = true
  default_uri_disabled = var.api_default_uri_disabled
  invoker_iam_disabled = false
  labels               = local.common_labels

  template {
    service_account                  = var.api_service_account_email
    timeout                          = "${var.api_timeout_seconds}s"
    max_instance_request_concurrency = var.api_concurrency

    scaling {
      min_instance_count = 0
      max_instance_count = var.api_max_instances
    }

    containers {
      image = local.image

      ports {
        container_port = 8080
      }

      dynamic "env" {
        for_each = local.api_plain_env

        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "AGENT_API_KEY_PEPPER"

        value_source {
          secret_key_ref {
            secret  = var.api_key_pepper_secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "AGENT_API_TASK_SIGNING_HMAC"

        value_source {
          secret_key_ref {
            secret  = var.task_signing_hmac_secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service" "worker" {
  provider = google-beta

  project              = var.project_id
  location             = var.region
  name                 = "${var.system_code}-${var.environment}-worker"
  ingress              = var.worker_ingress
  deletion_protection  = true
  invoker_iam_disabled = false
  labels               = local.common_labels

  template {
    service_account                  = var.worker_service_account_email
    timeout                          = "${var.worker_timeout_seconds}s"
    max_instance_request_concurrency = var.worker_concurrency

    scaling {
      min_instance_count = 0
      max_instance_count = var.worker_max_instances
    }

    containers {
      image = local.image

      ports {
        container_port = 8080
      }

      dynamic "env" {
        for_each = local.worker_plain_env

        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "AGENT_API_KEY_PEPPER"

        value_source {
          secret_key_ref {
            secret  = var.api_key_pepper_secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "AGENT_API_TASK_SIGNING_HMAC"

        value_source {
          secret_key_ref {
            secret  = var.task_signing_hmac_secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_tasks_queue" "dispatch" {
  project  = var.project_id
  location = var.region
  name     = local.dispatch_queue_name

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
      host   = replace(google_cloud_run_v2_service.worker.uri, "https://", "")

      path_override {
        path = var.worker_dispatch_path
      }
    }

    oidc_token {
      service_account_email = var.tasks_service_account_email
      audience              = google_cloud_run_v2_service.worker.uri
    }

    header_overrides {
      header {
        key   = "Content-Type"
        value = "application/json"
      }
    }
  }

  depends_on = [google_project_service.required]
}
