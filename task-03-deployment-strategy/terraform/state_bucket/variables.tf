variable "project_id" {
  description = "Existing Google Cloud project that owns the state bucket."
  type        = string
}

variable "region" {
  description = "Bucket location."
  type        = string
  default     = "europe-west3"
}

variable "bootstrap_state_bucket_name" {
  description = "Globally unique bucket name for the privileged bootstrap state."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.bootstrap_state_bucket_name))
    error_message = "bootstrap_state_bucket_name must be a valid GCS bucket name."
  }
}

variable "application_state_bucket_name" {
  description = "Globally unique bucket name for the application delivery state."
  type        = string

  validation {
    condition = (
      can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.application_state_bucket_name)) &&
      var.application_state_bucket_name != var.bootstrap_state_bucket_name
    )
    error_message = "application_state_bucket_name must be valid and different from bootstrap_state_bucket_name."
  }
}

variable "existing_state_buckets" {
  description = "Existing deterministic buckets that Terraform should import before reconciliation."
  type        = map(string)
  default     = {}

  validation {
    condition = (
      alltrue([for key in keys(var.existing_state_buckets) : contains(["application", "bootstrap"], key)]) &&
      alltrue([for name in values(var.existing_state_buckets) : contains([var.application_state_bucket_name, var.bootstrap_state_bucket_name], name)])
    )
    error_message = "existing_state_buckets may contain only the deterministic application and bootstrap buckets."
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
