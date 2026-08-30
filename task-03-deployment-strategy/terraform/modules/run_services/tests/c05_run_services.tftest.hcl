mock_provider "google" {}

mock_provider "google-beta" {}

variables {
  project_id                   = "contract-assignment-dev"
  region                       = "europe-west3"
  environment                  = "dev"
  system_code                  = "sai"
  artifact_registry_location   = "europe-west3"
  artifact_repository_id       = "assessment-images"
  image_digest                 = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  api_service_account_email    = "sai-dev-api@contract-assignment-dev.iam.gserviceaccount.com"
  worker_service_account_email = "sai-dev-worker@contract-assignment-dev.iam.gserviceaccount.com"
  tasks_service_account_email  = "sai-dev-tasks@contract-assignment-dev.iam.gserviceaccount.com"
  api_ingress                  = "INGRESS_TRAFFIC_ALL"
  api_default_uri_disabled     = false
  api_allow_unauthenticated    = true
  worker_ingress               = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  api_key_pepper_secret_id     = "sai-dev-api-key-pepper"
  task_signing_hmac_secret_id  = "sai-dev-task-signing-hmac"
}

run "default_contract_is_bounded_and_digest_pinned" {
  command = plan

  assert {
    condition     = output.api_service.min_instances == 0
    error_message = "API must scale to zero."
  }

  assert {
    condition     = output.worker_service.min_instances == 0
    error_message = "Worker must scale to zero."
  }

  assert {
    condition     = output.api_service.public_invoker_required
    error_message = "Baseline mode must declare the bootstrap-owned public invoker policy."
  }

  assert {
    condition     = !google_cloud_run_v2_service.api.invoker_iam_disabled
    error_message = "API must keep Cloud Run IAM checks enabled."
  }

  assert {
    condition     = !google_cloud_run_v2_service.worker.invoker_iam_disabled
    error_message = "Worker must keep Cloud Run IAM checks enabled."
  }

  assert {
    condition     = toset(output.worker_service.invoker_members) == toset(["serviceAccount:sai-dev-tasks@contract-assignment-dev.iam.gserviceaccount.com"])
    error_message = "Only the Cloud Tasks caller identity should invoke the worker."
  }

  assert {
    condition     = output.dispatch_queue.ownership == "bootstrap"
    error_message = "The application stack must use the bootstrap-owned dispatch queue."
  }

  assert {
    condition     = output.iam_contract.worker_invoker_binding_count == 1
    error_message = "The application contract must request one bootstrap-owned worker invoker binding."
  }

  assert {
    condition     = output.iam_contract.api_public_invoker_count == 1
    error_message = "Baseline mode must grant only the API public invoker binding."
  }

  assert {
    condition     = output.iam_contract.api_queue_enqueuer_member == "serviceAccount:sai-dev-api@contract-assignment-dev.iam.gserviceaccount.com"
    error_message = "Only the API runtime should enqueue onto the queue."
  }

  assert {
    condition = (
      output.iam_contract.api_tasks_identity_role == "roles/iam.serviceAccountUser" &&
      output.iam_contract.api_tasks_identity_member == output.iam_contract.api_queue_enqueuer_member
    )
    error_message = "The API task creator must be allowed to attach the dedicated OIDC identity."
  }

  assert {
    condition = toset(output.iam_contract.task_viewer_members) == toset([
      "serviceAccount:sai-dev-api@contract-assignment-dev.iam.gserviceaccount.com",
      "serviceAccount:sai-dev-worker@contract-assignment-dev.iam.gserviceaccount.com",
    ])
    error_message = "Only API and worker runtimes should inspect deterministic task state."
  }

  assert {
    condition = toset(output.iam_contract.task_deleter_members) == toset([
      "serviceAccount:sai-dev-api@contract-assignment-dev.iam.gserviceaccount.com",
      "serviceAccount:sai-dev-worker@contract-assignment-dev.iam.gserviceaccount.com",
    ])
    error_message = "Only API and worker runtimes should remove terminal or cancelled tasks."
  }

  assert {
    condition     = output.dispatch_queue.path == "projects/contract-assignment-dev/locations/europe-west3/queues/sai-dev-run-dispatch"
    error_message = "The execution plane must use the deterministic bootstrap queue path."
  }

  assert {
    condition     = output.api_service.image == "europe-west3-docker.pkg.dev/contract-assignment-dev/assessment-images/siemens-agent-api@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    error_message = "Cloud Run must reference the immutable image digest."
  }

  assert {
    condition     = output.worker_service.image == output.api_service.image
    error_message = "The worker contract must expose the same immutable image digest as the API."
  }
}

run "hardened_api_mode_blocks_url_bypass_and_keeps_lb_usable" {
  command = plan

  variables {
    api_ingress               = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    api_default_uri_disabled  = true
    api_allow_unauthenticated = false
  }

  assert {
    condition     = output.api_service.default_uri_disabled
    error_message = "Hardened mode must disable the default API URL."
  }

  assert {
    condition     = google_cloud_run_v2_service.api.launch_stage == "BETA"
    error_message = "Default URL disabling must declare the required Cloud Run launch stage."
  }

  assert {
    condition     = !output.api_service.public_invoker_required
    error_message = "Hardened mode must not grant unauthenticated Cloud Run invocation."
  }

  assert {
    condition     = output.iam_contract.api_public_invoker_count == 0
    error_message = "Hardened mode must create no public API IAM member."
  }

}

run "invalid_digest_fails_closed" {
  command = plan

  variables {
    image_digest = "latest"
  }

  expect_failures = [var.image_digest]
}

run "invalid_worker_path_fails_closed" {
  command = plan

  variables {
    worker_dispatch_path = "internal/tasks/run-delivery"
  }

  expect_failures = [var.worker_dispatch_path]
}

run "incoherent_hardened_ingress_fails_closed" {
  command = plan

  variables {
    api_ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  }

  expect_failures = [var.api_default_uri_disabled]
}

run "public_invoker_with_disabled_api_url_fails_closed" {
  command = plan

  variables {
    api_ingress               = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    api_default_uri_disabled  = true
    api_allow_unauthenticated = true
  }

  expect_failures = [var.api_allow_unauthenticated]
}

run "public_worker_ingress_fails_closed" {
  command = plan

  variables {
    worker_ingress = "INGRESS_TRAFFIC_ALL"
  }

  expect_failures = [var.worker_ingress]
}

run "external_api_identity_fails_closed" {
  command = plan

  variables {
    api_service_account_email = "external-api@attacker-project.iam.gserviceaccount.com"
  }

  expect_failures = [var.api_service_account_email]
}

run "external_worker_identity_fails_closed" {
  command = plan

  variables {
    worker_service_account_email = "external-worker@attacker-project.iam.gserviceaccount.com"
  }

  expect_failures = [var.worker_service_account_email]
}

run "external_tasks_identity_fails_closed" {
  command = plan

  variables {
    tasks_service_account_email = "external-tasks@attacker-project.iam.gserviceaccount.com"
  }

  expect_failures = [var.tasks_service_account_email]
}

run "collapsed_api_and_worker_identities_fail_closed" {
  command = plan

  variables {
    worker_service_account_email = "sai-dev-api@contract-assignment-dev.iam.gserviceaccount.com"
  }

  expect_failures = [var.worker_service_account_email]
}

run "collapsed_tasks_identity_fails_closed" {
  command = plan

  variables {
    tasks_service_account_email = "sai-dev-api@contract-assignment-dev.iam.gserviceaccount.com"
  }

  expect_failures = [var.tasks_service_account_email]
}

run "label_overflow_and_secret_collision_fail_closed" {
  command = plan

  variables {
    labels = {
      for index in range(62) : "label_${index}" => "value"
    }
    api_key_pepper_secret_id    = "same-secret"
    task_signing_hmac_secret_id = "same-secret"
  }

  expect_failures = [
    var.labels,
    var.task_signing_hmac_secret_id,
  ]
}
