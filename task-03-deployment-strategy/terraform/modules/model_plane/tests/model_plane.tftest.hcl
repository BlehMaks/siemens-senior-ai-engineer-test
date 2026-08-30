mock_provider "google" {}

mock_provider "google-beta" {}

variables {
  project_id = "production-model-plane"
  region     = "europe-west1"
}

run "assessment_profile_creates_no_model_capacity" {
  command = plan

  assert {
    condition     = !output.model_plane.enabled
    error_message = "The default assessment profile must disable the model plane."
  }

  assert {
    condition     = length(google_cloud_run_v2_service.model) == 0
    error_message = "Assessment mode must create no Cloud Run GPU service."
  }

  assert {
    condition     = length(google_project_service.run) == 0
    error_message = "Assessment mode must not enable a model-serving API."
  }
}

run "gpu_profile_is_private_bounded_and_digest_pinned" {
  command = plan

  variables {
    model_plane_profile            = "cloud_run_gpu"
    model_image                    = "europe-west1-docker.pkg.dev/production-model-plane/models/ollama-qwen@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    model_name                     = "qwen3:8b-2026-08"
    worker_service_account_email   = "sai-prod-worker@production-model-plane.iam.gserviceaccount.com"
    deployer_service_account_email = "sai-prod-deploy@production-model-plane.iam.gserviceaccount.com"
  }

  assert {
    condition     = output.model_plane.enabled
    error_message = "The explicit GPU profile must enable the model plane."
  }

  assert {
    condition     = google_cloud_run_v2_service.model[0].ingress == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    error_message = "The model service must reject public ingress."
  }

  assert {
    condition     = google_cloud_run_v2_service.model[0].template[0].scaling[0].min_instance_count == 0 && google_cloud_run_v2_service.model[0].template[0].scaling[0].max_instance_count == 1
    error_message = "The reference profile must scale to zero and cap GPU instances."
  }

  assert {
    condition     = google_cloud_run_v2_service.model[0].template[0].containers[0].resources[0].limits["nvidia.com/gpu"] == "1"
    error_message = "The reference service must request exactly one L4 GPU per instance."
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_binding.worker_invoker[0].members == toset(["serviceAccount:sai-prod-worker@production-model-plane.iam.gserviceaccount.com"])
    error_message = "Only the worker runtime may invoke the model service."
  }

  assert {
    condition     = output.model_plane.public_invoker_count == 0
    error_message = "The model plane must never declare a public invoker."
  }
}

run "gpu_profile_rejects_unpinned_or_missing_inputs" {
  command = plan

  variables {
    model_plane_profile = "cloud_run_gpu"
    model_image         = "europe-west1-docker.pkg.dev/production-model-plane/models/ollama:latest"
    model_name          = "latest"
  }

  expect_failures = [
    var.model_image,
    var.model_name,
    var.worker_service_account_email,
  ]
}

run "gpu_profile_requires_a_distinct_deployer" {
  command = plan

  variables {
    model_plane_profile          = "cloud_run_gpu"
    model_image                  = "europe-west1-docker.pkg.dev/production-model-plane/models/ollama-qwen@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    model_name                   = "qwen3:8b-2026-08"
    worker_service_account_email = "sai-prod-worker@production-model-plane.iam.gserviceaccount.com"
  }

  expect_failures = [var.deployer_service_account_email]
}

run "gpu_profile_rejects_an_external_worker" {
  command = plan

  variables {
    model_plane_profile            = "cloud_run_gpu"
    model_image                    = "europe-west1-docker.pkg.dev/production-model-plane/models/ollama-qwen@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    model_name                     = "qwen3:8b-2026-08"
    worker_service_account_email   = "sai-prod-worker@attacker-project.iam.gserviceaccount.com"
    deployer_service_account_email = "sai-prod-deploy@production-model-plane.iam.gserviceaccount.com"
  }

  expect_failures = [var.worker_service_account_email]
}

run "assessment_rejects_hidden_model_inputs" {
  command = plan

  variables {
    model_image = "europe-west1-docker.pkg.dev/production-model-plane/models/ollama-qwen@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    model_name  = "qwen3:8b-2026-08"
  }

  expect_failures = [
    var.model_image,
    var.model_name,
  ]
}
