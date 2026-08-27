mock_provider "google" {}

mock_provider "google-beta" {}

run "github_wif_plans_in_one_pass" {
  command = plan

  plan_options {
    refresh = false
  }

  variables {
    project_id           = "contract-assignment-dev"
    state_bucket_name    = "contract-assignment-dev-tf-state"
    enable_github_wif    = true
    github_repository    = "example-org/siemens-senior-ai-engineer-test"
    github_repository_id = "123456789"
    github_branch        = "main"
    github_environment   = "dev"
  }

  assert {
    condition = alltrue([
      for permission in google_project_iam_custom_role.deployer_application.permissions :
      !startswith(permission, "datastore.entities.")
    ])
    error_message = "The infrastructure deployer must not receive Firestore entity data-plane permissions."
  }

  assert {
    condition     = google_storage_bucket_iam_member.deployer_state_objects.role == "roles/storage.objectAdmin"
    error_message = "Terraform backend access must be object-only and bucket-scoped."
  }

  assert {
    condition = alltrue([
      for secret in google_secret_manager_secret.managed :
      secret.deletion_protection
    ])
    error_message = "Bootstrap-owned secret containers must survive routine application teardown."
  }

  assert {
    condition = alltrue([
      for permission in google_project_iam_custom_role.deployer_application.permissions :
      !contains(["run.routes.invoke", "run.services.sshRoot"], permission)
    ])
    error_message = "The deployer custom role must exclude direct Cloud Run invocation and SSH access."
  }

  assert {
    condition     = length(output.secret_accessors.api_key_pepper) == 2
    error_message = "Only the API and worker identities may read the API-key pepper."
  }

  assert {
    condition     = length(output.secret_accessors.task_signing_hmac) == 2
    error_message = "Only the signing and verifying workloads may read the task HMAC."
  }

  assert {
    condition = google_project_iam_custom_role.tasks_policy.permissions == toset([
      "iam.serviceAccounts.get",
      "iam.serviceAccounts.getIamPolicy",
      "iam.serviceAccounts.setIamPolicy",
    ])
    error_message = "The deployer may administer only the IAM policy needed for the tasks identity."
  }
}

run "invalid_or_colliding_secret_ids_fail_closed" {
  command = plan

  variables {
    project_id        = "contract-assignment-dev"
    state_bucket_name = "contract-assignment-dev-tf-state"
    enable_github_wif = false
    secret_ids = {
      api_key_pepper    = "same-secret"
      task_signing_hmac = "same-secret"
    }
  }

  expect_failures = [var.secret_ids]
}

run "disabled_wif_needs_no_repository_id" {
  command = plan

  variables {
    project_id        = "contract-assignment-dev"
    state_bucket_name = "contract-assignment-dev-tf-state"
    enable_github_wif = false
  }
}
