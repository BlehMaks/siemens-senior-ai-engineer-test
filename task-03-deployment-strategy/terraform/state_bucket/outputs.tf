output "state_bucket_names" {
  description = "Isolated GCS buckets for bootstrap and application Terraform state."
  value = {
    for scope, bucket in google_storage_bucket.terraform_state :
    scope => bucket.name
  }
}

output "project_number" {
  description = "Numeric identifier resolved by Terraform for the target project."
  value       = data.google_project.current.number
}
