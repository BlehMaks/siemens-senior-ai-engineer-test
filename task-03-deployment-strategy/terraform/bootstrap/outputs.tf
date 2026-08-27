output "state_bucket_name" {
  description = "GCS bucket that later Terraform stacks can use as their remote backend."
  value       = google_storage_bucket.terraform_state.name
}

output "workload_identity_provider_name" {
  description = "Full resource name of the GitHub provider, or null when federation is disabled."
  value = var.enable_github_wif ? (
    google_iam_workload_identity_pool_provider.github[0].name
  ) : null
}

output "service_accounts" {
  description = "Bootstrap workload identities keyed by workload name."
  value = merge({
    for name, identity in module.identity :
    name => {
      email = identity.email
      name  = identity.name
    }
    }, {
    deployer = {
      email = module.deployer_identity.email
      name  = module.deployer_identity.name
    }
  })
}

output "secret_containers" {
  description = "Bootstrap-owned secret IDs. Payloads and versions stay out of Terraform state."
  value = {
    for key, secret in google_secret_manager_secret.managed :
    key => secret.secret_id
  }
}

output "secret_accessors" {
  description = "Runtime identities with resource-scoped access to each bootstrap-owned secret."
  value = {
    api_key_pepper = sort([
      for binding in google_secret_manager_secret_iam_member.api_pepper_reader :
      binding.member
    ])
    task_signing_hmac = sort([
      for binding in google_secret_manager_secret_iam_member.task_hmac_reader :
      binding.member
    ])
  }
}

output "runtime_policy" {
  description = "Human-bootstrap queue and IAM applied after the deterministic services exist."
  value = {
    enabled                        = var.enable_runtime_policy
    api_public_invoker_enabled     = var.enable_runtime_policy && var.api_allow_unauthenticated
    dispatch_queue_count           = length(google_cloud_tasks_queue.dispatch)
    queue_binding_count            = length(google_cloud_tasks_queue_iam_member.runtime)
    tasks_service_agent_member     = google_service_account_iam_member.tasks_service_agent_token_creator.member
    tasks_service_agent_token_role = google_service_account_iam_member.tasks_service_agent_token_creator.role
    worker_invoker_binding_count   = length(google_cloud_run_v2_service_iam_binding.worker_invoker)
  }
}

output "dispatch_queue" {
  description = "Human-bootstrap-owned authenticated Cloud Tasks queue, or null before runtime policy is enabled."
  value = var.enable_runtime_policy ? {
    name                      = "${var.system_code}-${var.environment}-run-dispatch"
    location                  = var.region
    max_dispatches_per_second = var.queue_max_dispatches_per_second
    max_concurrent_dispatches = var.queue_max_concurrent_dispatches
    max_attempts              = var.queue_max_attempts
    max_retry_duration        = "${var.queue_max_retry_seconds}s"
    min_backoff               = "${var.queue_min_backoff_seconds}s"
    max_backoff               = "${var.queue_max_backoff_seconds}s"
    oidc_service_account      = module.identity["tasks"].email
    oidc_audience             = data.google_cloud_run_v2_service.worker[0].uri
    target_host               = replace(data.google_cloud_run_v2_service.worker[0].uri, "https://", "")
    target_path               = var.worker_dispatch_path
  } : null
}

output "remote_backend" {
  description = "Backend stanza values for later application stacks."
  value = {
    bucket = google_storage_bucket.terraform_state.name
    prefix = "assessment"
  }
}
