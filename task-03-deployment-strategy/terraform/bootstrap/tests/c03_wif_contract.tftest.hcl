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
}

run "disabled_wif_needs_no_repository_id" {
  command = plan

  variables {
    project_id        = "contract-assignment-dev"
    state_bucket_name = "contract-assignment-dev-tf-state"
    enable_github_wif = false
  }
}
