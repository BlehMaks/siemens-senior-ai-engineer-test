mock_provider "google" {}

mock_provider "google-beta" {}

variables {
  project_id                     = "contract-assignment-dev"
  project_number                 = "123456789012"
  billing_account_id             = "ABC123-DEF456-GHI789"
  budget_notification_emails     = ["cloud-budgets@example.com"]
  api_service_account_email      = "sai-dev-api@contract-assignment-dev.iam.gserviceaccount.com"
  worker_service_account_email   = "sai-dev-worker@contract-assignment-dev.iam.gserviceaccount.com"
  deployer_service_account_email = "sai-dev-deploy@contract-assignment-dev.iam.gserviceaccount.com"
  tasks_service_account_email    = "sai-dev-tasks@contract-assignment-dev.iam.gserviceaccount.com"
  image_digest                   = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}

run "dev_environment_plans_with_reviewed_contract" {
  command = plan

  assert {
    condition     = output.managed_services.budget.amount_units == "5"
    error_message = "The test environment must keep its billing alert at EUR 5."
  }

  assert {
    condition     = jsonencode(output.managed_services.budget.threshold_rules) == jsonencode([0.2, 0.5, 0.8, 1.0])
    error_message = "The test environment must alert before exhausting EUR 5."
  }

  assert {
    condition     = output.execution_plane.api_service.max_instances == 1
    error_message = "The test API must be capped at one scale-to-zero instance."
  }

  assert {
    condition     = output.execution_plane.worker_service.max_instances == 1
    error_message = "The test worker must be capped at one scale-to-zero instance."
  }
}

run "tasks_identity_cannot_collapse_into_deployer" {
  command = plan

  variables {
    tasks_service_account_email = "sai-dev-deploy@contract-assignment-dev.iam.gserviceaccount.com"
  }

  expect_failures = [var.tasks_service_account_email]
}

run "secret_ids_must_match_bootstrap_contract" {
  command = plan

  variables {
    secret_ids = {
      api_key_pepper    = "same-secret"
      task_signing_hmac = "same-secret"
    }
  }

  expect_failures = [var.secret_ids]
}
