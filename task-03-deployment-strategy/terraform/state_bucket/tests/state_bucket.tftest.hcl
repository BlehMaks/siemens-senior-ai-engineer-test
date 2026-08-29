mock_provider "google" {}

run "state_bucket_is_private_and_recoverable" {
  command = plan

  variables {
    project_id                    = "contract-assignment-dev"
    region                        = "europe-west3"
    bootstrap_state_bucket_name   = "contract-assignment-dev-sai-bootstrap-tf-state"
    application_state_bucket_name = "contract-assignment-dev-sai-app-tf-state"
  }

  assert {
    condition = alltrue([
      for bucket in google_storage_bucket.terraform_state :
      bucket.uniform_bucket_level_access
    ])
    error_message = "Both state buckets must use uniform bucket-level access."
  }

  assert {
    condition = alltrue([
      for bucket in google_storage_bucket.terraform_state :
      bucket.public_access_prevention == "enforced"
    ])
    error_message = "Both state buckets must prevent public access."
  }

  assert {
    condition = alltrue([
      for bucket in google_storage_bucket.terraform_state :
      bucket.versioning[0].enabled
    ])
    error_message = "Both state buckets must keep object versions."
  }

  assert {
    condition = alltrue([
      for bucket in google_storage_bucket.terraform_state :
      !bucket.force_destroy
    ])
    error_message = "Neither state bucket may be force-destroyed."
  }

  assert {
    condition     = length(google_storage_bucket.terraform_state) == 2
    error_message = "Bootstrap and application state must use separate buckets."
  }
}
