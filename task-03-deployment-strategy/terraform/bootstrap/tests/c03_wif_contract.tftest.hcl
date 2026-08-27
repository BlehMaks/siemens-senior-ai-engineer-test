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
      !contains(["run.routes.invoke", "run.services.sshRoot"], permission) &&
      !endswith(permission, ".setIamPolicy")
    ])
    error_message = "The deployer must exclude direct runtime access and every IAM-policy mutation."
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
    condition     = output.runtime_policy.tasks_service_agent_token_role == "roles/iam.serviceAccountTokenCreator"
    error_message = "The human bootstrap must own the Cloud Tasks token-minting grant."
  }

  assert {
    condition     = !output.runtime_policy.enabled && output.runtime_policy.queue_binding_count == 0 && output.runtime_policy.worker_invoker_binding_count == 0
    error_message = "Runtime policies must wait until the application resources exist."
  }
}

run "human_bootstrap_owns_runtime_policies" {
  command = plan

  plan_options {
    refresh = false
  }

  variables {
    project_id                = "contract-assignment-dev"
    state_bucket_name         = "contract-assignment-dev-tf-state"
    enable_github_wif         = false
    enable_runtime_policy     = true
    api_allow_unauthenticated = true
  }

  assert {
    condition     = output.runtime_policy.api_public_invoker_enabled
    error_message = "Baseline bootstrap policy must expose only the API invocation boundary."
  }

  assert {
    condition     = output.runtime_policy.worker_invoker_binding_count == 1
    error_message = "Only one bootstrap-owned worker invoker policy is expected."
  }

  assert {
    condition     = output.runtime_policy.queue_binding_count == 5
    error_message = "Bootstrap must apply the five reviewed queue runtime bindings."
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
