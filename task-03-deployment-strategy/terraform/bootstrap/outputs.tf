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

output "remote_backend" {
  description = "Backend stanza values for later application stacks."
  value = {
    bucket = google_storage_bucket.terraform_state.name
    prefix = "assessment"
  }
}
