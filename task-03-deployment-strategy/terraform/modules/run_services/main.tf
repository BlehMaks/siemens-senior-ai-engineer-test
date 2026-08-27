locals {
  required_services = toset([
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
