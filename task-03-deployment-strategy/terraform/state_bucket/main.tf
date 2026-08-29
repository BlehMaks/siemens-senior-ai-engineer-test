resource "google_storage_bucket" "terraform_state" {
  name                        = var.state_bucket_name
  location                    = var.region
  project                     = var.project_id
  storage_class               = "STANDARD"
  labels                      = var.labels
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }
}
