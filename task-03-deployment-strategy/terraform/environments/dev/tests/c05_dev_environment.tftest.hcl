mock_provider "google" {
  override_data {
    target = module.run_services.data.google_project.current
    values = {
      number = "123456789012"
    }
  }
}

mock_provider "google-beta" {}

variables {
  project_id                     = "contract-assignment-dev"
  project_number                 = "123456789012"
  api_service_account_email      = "sai-dev-api@contract-assignment-dev.iam.gserviceaccount.com"
  worker_service_account_email   = "sai-dev-worker@contract-assignment-dev.iam.gserviceaccount.com"
  deployer_service_account_email = "sai-dev-deploy@contract-assignment-dev.iam.gserviceaccount.com"
  tasks_service_account_email    = "sai-dev-tasks@contract-assignment-dev.iam.gserviceaccount.com"
  image_digest                   = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}

run "dev_environment_plans_with_reviewed_contract" {
  command = plan
}

run "tasks_identity_cannot_collapse_into_deployer" {
  command = plan

  variables {
    tasks_service_account_email = "sai-dev-deploy@contract-assignment-dev.iam.gserviceaccount.com"
  }

  expect_failures = [var.tasks_service_account_email]
}
