mock_provider "google" {}

mock_provider "google-beta" {}

variables {
  project_id                     = "contract-assignment-dev"
  project_number                 = "123456789012"
  region                         = "europe-west3"
  environment                    = "dev"
  system_code                    = "sai"
  billing_account_id             = "ABC123-DEF456-GHI789"
  budget_notification_emails     = ["cloud-budgets@example.com"]
  api_service_account_email      = "sai-dev-api@contract-assignment-dev.iam.gserviceaccount.com"
  worker_service_account_email   = "sai-dev-worker@contract-assignment-dev.iam.gserviceaccount.com"
  deployer_service_account_email = "sai-dev-deploy@contract-assignment-dev.iam.gserviceaccount.com"
}

run "default_contract_is_low_cost_and_container_only" {
  command = plan

  assert {
    condition     = output.firestore.location == "europe-west3"
    error_message = "Firestore must stay in the configured region."
  }

  assert {
    condition     = output.firestore.delete_protection_state == "DELETE_PROTECTION_ENABLED"
    error_message = "Firestore deletion protection must be enabled by default."
  }

  assert {
    condition     = output.firestore.deletion_policy == "ABANDON"
    error_message = "Firestore destroy must default to abandon."
  }

  assert {
    condition     = output.firestore.pitr == "POINT_IN_TIME_RECOVERY_ENABLED"
    error_message = "Firestore must retain point-in-time recovery by default."
  }

  assert {
    condition = alltrue([
      for secret in google_secret_manager_secret.managed :
      secret.deletion_protection
    ])
    error_message = "Secret containers must survive routine Terraform destroy."
  }

  assert {
    condition     = output.artifact_registry.immutable_tags
    error_message = "Artifact Registry must pin immutable tags."
  }

  assert {
    condition     = output.artifact_registry.writer_role == "roles/artifactregistry.writer"
    error_message = "Only the deployer should receive repository writer access."
  }

  assert {
    condition     = output.logging.retention_days == 30
    error_message = "Application logs must use bounded retention."
  }

  assert {
    condition     = output.logging.location == "europe-west3"
    error_message = "Application logs must stay in the configured region."
  }

  assert {
    condition = toset(output.workload_access.secret_accessors.api_key_pepper) == toset([
      "serviceAccount:sai-dev-api@contract-assignment-dev.iam.gserviceaccount.com",
      "serviceAccount:sai-dev-worker@contract-assignment-dev.iam.gserviceaccount.com",
    ])
    error_message = "Only the API and worker identities may read the API-key pepper."
  }

  assert {
    condition = toset(output.workload_access.secret_accessors.task_signing_hmac) == toset([
      "serviceAccount:sai-dev-api@contract-assignment-dev.iam.gserviceaccount.com",
      "serviceAccount:sai-dev-worker@contract-assignment-dev.iam.gserviceaccount.com",
    ])
    error_message = "Only the signing and verifying workloads may read the task HMAC."
  }

  assert {
    condition = toset(output.firestore.composite_indexes) == toset([
      "run_events",
      "runs",
      "sessions",
    ])
    error_message = "Managed Firestore must provision the composite indexes required by repository queries."
  }

  assert {
    condition     = output.budget.enabled
    error_message = "Budget should be enabled when billing coordinates are provided."
  }

  assert {
    condition     = output.budget.amount_units == "10"
    error_message = "The default monthly budget cap should remain EUR 10."
  }

  assert {
    condition     = output.budget.default_recipients_disabled
    error_message = "Budget alerts must not notify broad default IAM recipients."
  }
}

run "no_billing_coordinates_means_no_budget_resource" {
  command = plan

  variables {
    project_number      = ""
    billing_account_id  = ""
    budget_amount_units = 0
  }

  assert {
    condition     = output.budget.enabled == false
    error_message = "Budget resources must stay absent without explicit billing inputs."
  }
}

run "billing_coordinates_without_recipient_mean_no_budget_resource" {
  command = plan

  variables {
    budget_notification_emails = []
  }

  assert {
    condition     = output.budget.enabled == false
    error_message = "A budget without an explicit recipient must not create silent alerts."
  }
}

run "custom_secret_ids_and_email_channels_are_accepted" {
  command = plan

  variables {
    billing_account_id = "ABC123-DEF456-GHI789"
    secret_ids = {
      api_key_pepper    = "contract-dev-api-pepper"
      task_signing_hmac = "contract-dev-task-hmac"
    }
    budget_notification_emails = [
      "cloud-budgets@example.com",
    ]
  }

  assert {
    condition     = output.secret_containers.api_key_pepper == "contract-dev-api-pepper"
    error_message = "Secret container IDs should remain caller-controlled."
  }

  assert {
    condition     = output.budget.channel_count == 1
    error_message = "Budget notifications should only exist for explicitly provided recipients."
  }
}

run "invalid_resource_and_identity_names_fail_closed" {
  command = plan

  variables {
    project_id  = "INVALID_PROJECT"
    region      = "global"
    environment = "qa"
    system_code = "TOO_LONG_SYSTEM"
  }

  expect_failures = [
    var.project_id,
    var.region,
    var.environment,
    var.system_code,
  ]
}

run "invalid_identity_syntax_fails_closed" {
  command = plan

  variables {
    api_service_account_email    = "api@example.com"
    worker_service_account_email = "worker@example.com"
  }

  expect_failures = [
    var.api_service_account_email,
    var.worker_service_account_email,
  ]
}

run "invalid_deployer_identity_syntax_fails_closed" {
  command = plan

  variables {
    deployer_service_account_email = "deployer@example.com"
  }

  expect_failures = [
    var.deployer_service_account_email,
  ]
}

run "invalid_policy_inputs_fail_closed" {
  command = plan

  variables {
    labels = {
      Owner = "Platform Team"
    }
    secret_ids = {
      api_key_pepper    = "INVALID_SECRET"
      task_signing_hmac = "ok-secret-id"
    }
    budget_alert_thresholds = [0.9, 0.5]
    budget_notification_emails = [
      "not-an-email",
    ]
  }

  expect_failures = [
    var.labels,
    var.secret_ids,
    var.budget_alert_thresholds,
    var.budget_notification_emails,
  ]
}

run "external_project_identities_fail_closed" {
  command = plan

  variables {
    api_service_account_email    = "external-api@attacker-project.iam.gserviceaccount.com"
    worker_service_account_email = "external-worker@attacker-project.iam.gserviceaccount.com"
  }

  expect_failures = [
    var.api_service_account_email,
    var.worker_service_account_email,
  ]
}

run "external_project_deployer_fails_closed" {
  command = plan

  variables {
    deployer_service_account_email = "external-deploy@attacker-project.iam.gserviceaccount.com"
  }

  expect_failures = [
    var.deployer_service_account_email,
  ]
}

run "collapsed_workload_identities_fail_closed" {
  command = plan

  variables {
    api_service_account_email      = "shared-runtime@contract-assignment-dev.iam.gserviceaccount.com"
    worker_service_account_email   = "shared-runtime@contract-assignment-dev.iam.gserviceaccount.com"
    deployer_service_account_email = "shared-runtime@contract-assignment-dev.iam.gserviceaccount.com"
  }

  expect_failures = [var.deployer_service_account_email]
}

run "resource_collisions_and_label_overflow_fail_closed" {
  command = plan

  variables {
    labels = {
      for index in range(62) : "label_${index}" => "value"
    }
    secret_ids = {
      api_key_pepper    = "same-secret"
      task_signing_hmac = "same-secret"
    }
  }

  expect_failures = [
    var.labels,
    var.secret_ids,
  ]
}
