locals {
  enabled = var.model_plane_profile == "cloud_run_gpu"
  common_labels = merge(var.labels, {
    component   = "model-plane"
    environment = var.environment
    managed_by  = "terraform"
    system      = var.system_code
  })
  service_name = "${var.system_code}-${var.environment}-model"
}

resource "google_project_service" "run" {
  count = local.enabled ? 1 : 0

  project            = var.project_id
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_service_account" "model" {
  count = local.enabled ? 1 : 0

  project      = var.project_id
  account_id   = local.service_name
  display_name = "Regional model runtime"
  description  = "Dedicated identity for the private Cloud Run GPU model service."
}

resource "google_service_account_iam_member" "deployer_runtime_user" {
  count = local.enabled ? 1 : 0

  service_account_id = google_service_account.model[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account_email}"
}

resource "google_cloud_run_v2_service" "model" {
  count    = local.enabled ? 1 : 0
  provider = google-beta

  project              = var.project_id
  location             = var.region
  name                 = local.service_name
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  launch_stage         = "BETA"
  deletion_protection  = true
  invoker_iam_disabled = false
  labels               = local.common_labels

  template {
    service_account                  = google_service_account.model[0].email
    timeout                          = "300s"
    max_instance_request_concurrency = 1
    gpu_zonal_redundancy_disabled    = var.gpu_zonal_redundancy_disabled

    scaling {
      min_instance_count = var.model_min_instances
      max_instance_count = var.model_max_instances
    }

    node_selector {
      accelerator = "nvidia-l4"
    }

    containers {
      image = var.model_image

      ports {
        container_port = 11434
      }

      resources {
        limits = {
          cpu              = "4"
          memory           = "16Gi"
          "nvidia.com/gpu" = "1"
        }
        cpu_idle          = false
        startup_cpu_boost = true
      }

      env {
        name  = "OLLAMA_HOST"
        value = "0.0.0.0:11434"
      }

      env {
        name  = "SAI_MODEL_NAME"
        value = var.model_name
      }

      startup_probe {
        initial_delay_seconds = 0
        timeout_seconds       = 5
        period_seconds        = 10
        failure_threshold     = 24

        tcp_socket {
          port = 11434
        }
      }
    }
  }

  depends_on = [
    google_project_service.run,
    google_service_account_iam_member.deployer_runtime_user,
  ]
}

resource "google_cloud_run_v2_service_iam_binding" "worker_invoker" {
  count = local.enabled ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.model[0].name
  role     = "roles/run.invoker"
  members  = ["serviceAccount:${var.worker_service_account_email}"]
}
