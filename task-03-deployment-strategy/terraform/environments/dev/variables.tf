variable "project_id" {
  description = "Assessment GCP project ID."
  type        = string
}

variable "project_number" {
  description = "Assessment GCP project number for Cloud Tasks and budget filters."
  type        = string
}

variable "region" {
  description = "Single assessment region."
  type        = string
  default     = "europe-west3"
}

variable "system_code" {
  description = "Short system prefix reused across resources."
  type        = string
  default     = "sai"
}

variable "labels" {
  description = "Environment labels applied to all managed services."
  type        = map(string)
  default = {
    owner = "platform"
  }
}

variable "billing_account_id" {
  description = "Optional billing account for Cloud Billing budget alerts."
  type        = string
  default     = ""
}

variable "budget_notification_emails" {
  description = "Optional budget alert recipients."
  type        = set(string)
  default     = []
}

variable "api_service_account_email" {
  description = "Bootstrap API runtime identity email."
  type        = string
}

variable "worker_service_account_email" {
  description = "Bootstrap worker runtime identity email."
  type        = string
}

variable "deployer_service_account_email" {
  description = "Bootstrap deployer identity email."
  type        = string
}

variable "tasks_service_account_email" {
  description = "Bootstrap Cloud Tasks caller identity email."
  type        = string

  validation {
    condition     = var.tasks_service_account_email != var.deployer_service_account_email
    error_message = "tasks_service_account_email must differ from the deployer identity."
  }
}

variable "secret_ids" {
  description = "Secret IDs created by the bootstrap stack and mounted into both runtimes."
  type = object({
    api_key_pepper    = string
    task_signing_hmac = string
  })
  default = {
    api_key_pepper    = "sai-dev-api-key-pepper"
    task_signing_hmac = "sai-dev-task-signing-hmac"
  }

  validation {
    condition = (
      length(toset([var.secret_ids.api_key_pepper, var.secret_ids.task_signing_hmac])) == 2 &&
      alltrue([
        for secret_id in [var.secret_ids.api_key_pepper, var.secret_ids.task_signing_hmac] :
        can(regex("^[a-z][a-z0-9-]{2,254}$", secret_id))
      ])
    )
    error_message = "Secret IDs must be unique and satisfy Secret Manager naming rules."
  }
}

variable "image_digest" {
  description = "Immutable OCI image digest promoted into both Cloud Run services."
  type        = string
}

variable "ingress_mode" {
  description = "Budget baseline or hardened LB+Armor ingress posture."
  type        = string
  default     = "baseline"
}
