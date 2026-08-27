variable "project_id" {
  description = "Assessment GCP project ID."
  type        = string
}

variable "project_number" {
  description = "Assessment GCP project number for budget filters."
  type        = string
  default     = ""
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
