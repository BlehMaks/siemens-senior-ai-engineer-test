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
  description = "Human-bootstrap IAM applied after the deterministic service and queue names exist."
  value = {
    enabled                        = var.enable_runtime_policy
    api_public_invoker_enabled     = var.enable_runtime_policy && var.api_allow_unauthenticated
    queue_binding_count            = length(google_cloud_tasks_queue_iam_member.runtime)
    tasks_service_agent_member     = google_service_account_iam_member.tasks_service_agent_token_creator.member
    tasks_service_agent_token_role = google_service_account_iam_member.tasks_service_agent_token_creator.role
    worker_invoker_binding_count   = length(google_cloud_run_v2_service_iam_binding.worker_invoker)
  }
}

output "remote_backend" {
  description = "Backend stanza values for later application stacks."
  value = {
    bucket = google_storage_bucket.terraform_state.name
    prefix = "assessment"
  }
}
