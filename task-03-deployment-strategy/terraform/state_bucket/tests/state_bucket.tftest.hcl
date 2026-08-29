mock_provider "google" {}

run "state_bucket_is_private_and_recoverable" {
  command = plan

  variables {
    project_id        = "contract-assignment-dev"
    region            = "europe-west3"
    state_bucket_name = "contract-assignment-dev-sai-tf-state"
  }

  assert {
    condition     = google_storage_bucket.terraform_state.uniform_bucket_level_access
    error_message = "The state bucket must use uniform bucket-level access."
  }

  assert {
    condition     = google_storage_bucket.terraform_state.public_access_prevention == "enforced"
    error_message = "The state bucket must prevent public access."
  }

  assert {
    condition     = google_storage_bucket.terraform_state.versioning[0].enabled
    error_message = "The state bucket must keep object versions."
  }

  assert {
    condition     = !google_storage_bucket.terraform_state.force_destroy
    error_message = "The state bucket must not be force-destroyed."
  }
}
