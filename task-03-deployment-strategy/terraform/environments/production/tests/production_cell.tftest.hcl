mock_provider "google" {}

mock_provider "google-beta" {}

variables {
  project_id                     = "contract-production-cell"
  project_number                 = "123456789012"
  region                         = "europe-west4"
  billing_account_id             = "ABC123-DEF456-GHI789"
  budget_amount_units            = 1000
  budget_notification_emails     = ["cloud-budgets@example.com"]
  api_service_account_email      = "sai-prod-api@contract-production-cell.iam.gserviceaccount.com"
  worker_service_account_email   = "sai-prod-worker@contract-production-cell.iam.gserviceaccount.com"
  deployer_service_account_email = "sai-prod-deploy@contract-production-cell.iam.gserviceaccount.com"
  tasks_service_account_email    = "sai-prod-tasks@contract-production-cell.iam.gserviceaccount.com"
  secret_ids = {
    api_key_pepper    = "sai-prod-api-key-pepper"
    task_signing_hmac = "sai-prod-task-signing-hmac"
  }
  image_digest        = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  model_image         = "europe-west4-docker.pkg.dev/contract-production-cell/models/ollama@sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
  model_name          = "granite3.3:8b-q4"
  model_min_instances = 1
  model_max_instances = 2
  model_concurrency   = 4
}

run "production_cell_wires_private_model_to_real_worker" {
  command = plan

  assert {
    condition = (
      output.production_cell.worker_service.runtime.inference_mode == "ollama" &&
      output.production_cell.worker_service.runtime.model_transport_profile == "cloud" &&
      output.production_cell.worker_service.runtime.model_base_url == output.production_cell.model_plane.service_uri &&
      output.production_cell.worker_service.runtime.model_google_id_token_audience == output.production_cell.model_plane.service_uri &&
      output.production_cell.worker_service.runtime.model_name == output.production_cell.model_plane.model_name &&
      jsonencode(output.production_cell.worker_service.runtime.search_backends) == jsonencode(["yahoo", "auto"])
    )
    error_message = "Production must wire the private model and resilient search order into the real worker."
  }

  assert {
    condition = (
      output.production_cell.model_plane.worker_invoker_member == "serviceAccount:sai-prod-worker@contract-production-cell.iam.gserviceaccount.com" &&
      output.production_cell.model_plane.public_invoker_count == 0
    )
    error_message = "Only the worker identity may invoke the private model service."
  }

  assert {
    condition = (
      output.production_cell.ingress_policy.mode == "hardened" &&
      output.production_cell.api_service.public_invoker_required == false
    )
    error_message = "Production API ingress must use the hardened policy."
  }
}
