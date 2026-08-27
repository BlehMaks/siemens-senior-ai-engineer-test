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
    condition = google_project_iam_custom_role.tasks_policy.permissions == toset([
      "iam.serviceAccounts.get",
      "iam.serviceAccounts.getIamPolicy",
      "iam.serviceAccounts.setIamPolicy",
    ])
    error_message = "The deployer may administer only the IAM policy needed for the tasks identity."
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
