locals {
  state_buckets = {
    application = var.application_state_bucket_name
    bootstrap   = var.bootstrap_state_bucket_name
  }
}

import {
  for_each = var.existing_state_buckets

  to = google_storage_bucket.terraform_state[each.key]
  id = each.value
}

resource "google_storage_bucket" "terraform_state" {
  for_each = local.state_buckets

  name          = each.value
  location      = var.region
  project       = var.project_id
  storage_class = "STANDARD"
  labels = merge(var.labels, {
    state_scope = each.key
  })
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }
}
