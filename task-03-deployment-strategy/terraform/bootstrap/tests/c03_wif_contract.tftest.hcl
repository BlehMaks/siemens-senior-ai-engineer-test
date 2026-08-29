mock_provider "google" {
  override_data {
    target = data.google_project.current
    values = {
      number = "123456789012"
    }
  }
}

mock_provider "google-beta" {}

mock_provider "github" {
  override_data {
    target = data.github_repository.target
    values = {
      name      = "siemens-senior-ai-engineer-test"
      full_name = "example-org/siemens-senior-ai-engineer-test"
      repo_id   = 123456789
    }
  }

  override_data {
    target = data.github_user.reviewer
    values = {
      id    = 24680
      login = "example-reviewer"
    }
  }
}

run "github_wif_plans_in_one_pass" {
  command = plan

  plan_options {
    refresh = false
  }

  variables {
    project_id         = "contract-assignment-dev"
    state_bucket_name  = "contract-assignment-dev-tf-state"
    enable_github_wif  = true
    github_repository  = "example-org/siemens-senior-ai-engineer-test"
    github_branch      = "main"
    github_environment = "dev"
    github_reviewer    = "example-reviewer"
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
    condition = toset(google_project_iam_custom_role.deployer_cloud_run_iam.permissions) == toset([
      "run.services.getIamPolicy",
      "run.services.setIamPolicy",
    ])
    error_message = "The conditional Cloud Run IAM role must contain only policy read and write permissions."
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
    condition     = output.runtime_policy.dispatch_queue_count == 1 && output.runtime_policy.queue_binding_count == 5
    error_message = "The queue and its five runtime bindings must be ready before the first application deployment."
  }


  assert {
    condition     = output.github_delivery.repository_id == "123456789" && length(output.github_delivery.variables) >= 10
    error_message = "GitHub delivery settings must derive the immutable repository ID and manage the environment variables."
  }
}

run "human_bootstrap_owns_queue_and_token_policy" {
  command = plan

  plan_options {
    refresh = false
  }

  variables {
    project_id        = "contract-assignment-dev"
    state_bucket_name = "contract-assignment-dev-tf-state"
    enable_github_wif = false
  }

  assert {
    condition     = output.runtime_policy.dispatch_queue_count == 1
    error_message = "The human bootstrap must own the single authenticated dispatch queue."
  }

  assert {
    condition     = output.runtime_policy.queue_binding_count == 5
    error_message = "Bootstrap must apply the five reviewed queue runtime bindings."
  }

  assert {
    condition     = output.dispatch_queue.max_concurrent_dispatches == 1
    error_message = "Queue concurrency must stay tightly bounded by default."
  }
}

run "inverted_queue_backoff_fails_closed" {
  command = plan

  variables {
    project_id                = "contract-assignment-dev"
    state_bucket_name         = "contract-assignment-dev-tf-state"
    enable_github_wif         = false
    queue_min_backoff_seconds = 100
    queue_max_backoff_seconds = 60
  }

  expect_failures = [var.queue_max_backoff_seconds]
}

run "unbounded_retry_window_fails_closed" {
  command = plan

  variables {
    project_id              = "contract-assignment-dev"
    state_bucket_name       = "contract-assignment-dev-tf-state"
    enable_github_wif       = false
    queue_max_retry_seconds = 0
  }

  expect_failures = [var.queue_max_retry_seconds]
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

run "disabled_wif_needs_no_github_reviewer" {
  command = plan

  variables {
    project_id        = "contract-assignment-dev"
    state_bucket_name = "contract-assignment-dev-tf-state"
    enable_github_wif = false
  }
}
