output "state_bucket_names" {
  description = "Foundation-owned state buckets with isolated bootstrap and application access."
  value = {
    application = var.application_state_bucket_name
    bootstrap   = var.bootstrap_state_bucket_name
  }
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
  description = "Bootstrap-owned queue, identity, and runtime access policy."
  value = {
    dispatch_queue_count            = 1
    firestore_database_name         = local.firestore_database_name
    firestore_runtime_binding_count = length(google_project_iam_member.runtime_firestore_user)
    firestore_index_binding_count   = 1
    queue_binding_count             = length(google_cloud_tasks_queue_iam_member.runtime)
    tasks_service_agent_member      = google_service_account_iam_member.tasks_service_agent_token_creator.member
    tasks_service_agent_token_role  = google_service_account_iam_member.tasks_service_agent_token_creator.role
    cloud_run_iam_enabled           = var.enable_runtime_policy
    worker_invoker_binding_count    = length(google_cloud_run_v2_service_iam_binding.worker_invoker)
    api_public_invoker_count        = length(google_cloud_run_v2_service_iam_member.api_public_invoker)
  }
}

output "dispatch_queue" {
  description = "Bootstrap-owned authenticated Cloud Tasks queue."
  value = {
    name                      = "${var.system_code}-${var.environment}-run-dispatch"
    location                  = var.region
    max_dispatches_per_second = var.queue_max_dispatches_per_second
    max_concurrent_dispatches = var.queue_max_concurrent_dispatches
    max_attempts              = var.queue_max_attempts
    max_retry_duration        = "${var.queue_max_retry_seconds}s"
    min_backoff               = "${var.queue_min_backoff_seconds}s"
    max_backoff               = "${var.queue_max_backoff_seconds}s"
    oidc_service_account      = module.identity["tasks"].email
    oidc_audience             = local.worker_service_url
    target_host               = replace(local.worker_service_url, "https://", "")
    target_path               = var.worker_dispatch_path
  }
}

output "remote_backend" {
  description = "Backend stanza values for later application stacks."
  value = {
    bucket = var.application_state_bucket_name
    prefix = "assessment"
  }
}

output "github_delivery" {
  description = "Terraform-managed GitHub delivery boundary, or null when GitHub WIF is disabled."
  value = var.enable_github_wif ? {
    repository_id = tostring(data.github_repository.target[0].repo_id)
    repository    = var.github_repository
    branch        = var.github_branch
    branch_protection = {
      admin_enforcement       = github_branch_protection.delivery[0].enforce_admins
      allows_deletions        = github_branch_protection.delivery[0].allows_deletions
      allows_force_pushes     = github_branch_protection.delivery[0].allows_force_pushes
      required_linear_history = github_branch_protection.delivery[0].required_linear_history
    }
    environment = github_repository_environment.deployment[0].environment
    reviewer    = var.github_reviewer
    variables   = sort(keys(github_actions_environment_variable.delivery))
  } : null
}
