output "state_bucket_names" {
  description = "Isolated GCS buckets for bootstrap and application Terraform state."
  value = {
    for scope, bucket in google_storage_bucket.terraform_state :
    scope => bucket.name
  }
}
