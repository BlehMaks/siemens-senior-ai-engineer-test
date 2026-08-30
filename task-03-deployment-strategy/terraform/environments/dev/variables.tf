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

variable "budget_amount_units" {
  description = "Test deployment budget in whole EUR units."
  type        = number
  default     = 5

  validation {
    condition     = floor(var.budget_amount_units) == var.budget_amount_units && var.budget_amount_units >= 1 && var.budget_amount_units <= 5
    error_message = "The dev budget must be a whole number from 1 through 5 EUR."
  }
}

variable "budget_alert_thresholds" {
  description = "Early alert thresholds for the test deployment budget."
  type        = list(number)
  default     = [0.2, 0.5, 0.8, 1.0]

  validation {
    condition = (
      length(var.budget_alert_thresholds) == 4 &&
      try(var.budget_alert_thresholds[0] == 0.2, false) &&
      try(var.budget_alert_thresholds[1] == 0.5, false) &&
      try(var.budget_alert_thresholds[2] == 0.8, false) &&
      try(var.budget_alert_thresholds[3] == 1.0, false)
    )
    error_message = "The dev environment requires the 20%, 50%, 80%, and 100% budget alerts."
  }
}

variable "api_max_instances" {
  description = "Maximum API replicas in the cost-bounded dev environment."
  type        = number
  default     = 1

  validation {
    condition     = var.api_max_instances == 1
    error_message = "The dev API must keep its one-instance cost cap."
  }
}

variable "worker_max_instances" {
  description = "Maximum worker replicas in the cost-bounded dev environment."
  type        = number
  default     = 1

  validation {
    condition     = var.worker_max_instances == 1
    error_message = "The dev worker must keep its one-instance cost cap."
  }
}

variable "model_plane_profile" {
  description = "The assessment deploy cannot create paid model-serving capacity."
  type        = string
  default     = "assessment"

  validation {
    condition     = var.model_plane_profile == "assessment"
    error_message = "Use the separate production-model-plane root for model serving."
  }
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
