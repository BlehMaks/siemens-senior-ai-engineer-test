variable "project_id" {
  description = "Production cell project ID."
  type        = string
}

variable "project_number" {
  description = "Production project number for Cloud Tasks and budget filters."
  type        = string
}

variable "region" {
  description = "Approved Cloud Run GPU and data region."
  type        = string
  default     = "europe-west4"
}

variable "environment" {
  description = "Production-like environment suffix."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be staging or prod."
  }
}

variable "system_code" {
  description = "Short resource prefix."
  type        = string
  default     = "sai"
}

variable "labels" {
  description = "Additional production labels."
  type        = map(string)
  default = {
    owner = "ml-platform"
  }
}

variable "billing_account_id" {
  description = "Billing account used for the production budget."
  type        = string
}

variable "budget_amount_units" {
  description = "Reviewed alert budget in whole EUR units."
  type        = number

  validation {
    condition     = floor(var.budget_amount_units) == var.budget_amount_units && var.budget_amount_units > 0
    error_message = "budget_amount_units must be a positive whole number."
  }
}

variable "budget_alert_thresholds" {
  description = "Production budget alert thresholds."
  type        = list(number)
  default     = [0.2, 0.5, 0.8, 1.0]
}

variable "budget_notification_emails" {
  description = "Budget alert recipients."
  type        = set(string)

  validation {
    condition     = length(var.budget_notification_emails) > 0
    error_message = "production requires at least one budget notification email."
  }
}

variable "api_service_account_email" {
  description = "Production API runtime identity."
  type        = string
}

variable "worker_service_account_email" {
  description = "Production worker identity and sole model invoker."
  type        = string
}

variable "deployer_service_account_email" {
  description = "Terraform deployer identity."
  type        = string
}

variable "tasks_service_account_email" {
  description = "Cloud Tasks OIDC caller identity."
  type        = string

  validation {
    condition     = var.tasks_service_account_email != var.deployer_service_account_email
    error_message = "tasks_service_account_email must differ from the deployer identity."
  }
}

variable "secret_ids" {
  description = "Bootstrap-owned secret IDs mounted into API and worker."
  type = object({
    api_key_pepper    = string
    task_signing_hmac = string
  })

  validation {
    condition     = var.secret_ids.api_key_pepper != var.secret_ids.task_signing_hmac
    error_message = "API pepper and task signing HMAC must be distinct secrets."
  }
}

variable "image_digest" {
  description = "Immutable agent API/worker image digest."
  type        = string
}

variable "model_image" {
  description = "Immutable Ollama-compatible model-serving image."
  type        = string
}

variable "model_name" {
  description = "Approved immutable model identifier."
  type        = string
}

variable "model_min_instances" {
  description = "Capacity-reviewed warm GPU floor."
  type        = number
}

variable "model_max_instances" {
  description = "Capacity-reviewed GPU ceiling."
  type        = number
}

variable "model_concurrency" {
  description = "Load-tested request concurrency per GPU instance."
  type        = number
}

variable "gpu_zonal_redundancy_disabled" {
  description = "Disable GPU zonal redundancy only after an availability review."
  type        = bool
  default     = false
}

variable "api_max_instances" {
  description = "Capacity-reviewed API replica ceiling."
  type        = number
  default     = 3
}

variable "worker_max_instances" {
  description = "Capacity-reviewed worker replica ceiling."
  type        = number
  default     = 5
}

variable "search_backends" {
  description = "Ordered web-search fallback list."
  type        = list(string)
  default     = ["yahoo", "auto"]

  validation {
    condition = (
      length(var.search_backends) >= 1 &&
      length(var.search_backends) <= 2 &&
      length(distinct(var.search_backends)) == length(var.search_backends) &&
      alltrue([for backend in var.search_backends : contains(["auto", "brave", "duckduckgo", "yahoo"], backend)])
    )
    error_message = "search_backends must contain one or two unique supported backend names."
  }
}

variable "action_log_level" {
  description = "Structured action-log verbosity."
  type        = string
  default     = "INFO"
}
