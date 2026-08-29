output "state_bucket_name" {
  description = "GCS bucket used by the remote Terraform backends."
  value       = google_storage_bucket.terraform_state.name
}
