variable "project_id" {
  description = "Existing Google Cloud project that owns the state bucket."
  type        = string
}

variable "region" {
  description = "Bucket location."
  type        = string
  default     = "europe-west3"
}

variable "state_bucket_name" {
  description = "Globally unique name for the Terraform state bucket."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid GCS bucket name."
  }
}

variable "labels" {
  description = "Labels applied to the state bucket."
  type        = map(string)
  default = {
    component   = "terraform-state"
    environment = "dev"
    managed_by  = "terraform"
    system      = "sai"
  }
}
