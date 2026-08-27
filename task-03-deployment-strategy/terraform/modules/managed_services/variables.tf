variable "project_id" {
  description = "GCP project ID that hosts the managed assessment resources."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid lowercase GCP project identifier."
  }
}

variable "project_number" {
  description = "Numeric GCP project number used by Cloud Billing budget filters."
  type        = string
  default     = ""

  validation {
    condition     = var.project_number == "" || can(regex("^[1-9][0-9]*$", var.project_number))
    error_message = "project_number must be empty or a positive numeric project number."
  }
}

variable "region" {
  description = "Primary region for regional assessment resources."
  type        = string

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
    error_message = "region must look like a GCP region such as europe-west3."
  }
}

variable "environment" {
  description = "Short environment name used in labels and resource names."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of dev, staging, or prod."
  }
}

variable "system_code" {
  description = "Short lowercase system prefix reused across assessment resources."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,8}$", var.system_code))
    error_message = "system_code must be 2-9 lowercase characters, digits, or hyphens."
  }
}

variable "labels" {
  description = "Short lowercase labels applied to managed resources."
  type        = map(string)
  default     = {}

  validation {
    condition = (
      length(var.labels) <= 61 &&
      alltrue([
        for key, value in var.labels :
        can(regex("^[a-z][a-z0-9_-]{0,62}$", key)) &&
        can(regex("^[a-z0-9_-]{1,63}$", value)) &&
        !contains(["environment", "managed_by", "system"], key)
      ])
    )
    error_message = "labels may contain at most 61 entries and must not override reserved module labels."
  }
}

variable "firestore_type" {
  description = "Firestore database mode for the assessment cell."
  type        = string
  default     = "FIRESTORE_NATIVE"

  validation {
    condition     = contains(["FIRESTORE_NATIVE", "DATASTORE_MODE"], var.firestore_type)
    error_message = "firestore_type must be FIRESTORE_NATIVE or DATASTORE_MODE."
  }
}

variable "firestore_delete_protection_state" {
  description = "Deletion protection mode for the Firestore database."
  type        = string
  default     = "DELETE_PROTECTION_ENABLED"

  validation {
    condition = contains([
      "DELETE_PROTECTION_ENABLED",
      "DELETE_PROTECTION_DISABLED",
    ], var.firestore_delete_protection_state)
    error_message = "firestore_delete_protection_state must be an allowed Firestore deletion protection value."
  }
}

variable "firestore_deletion_policy" {
  description = "Terraform destroy behavior for the Firestore database."
  type        = string
  default     = "ABANDON"

  validation {
    condition     = contains(["ABANDON", "DELETE"], var.firestore_deletion_policy)
    error_message = "firestore_deletion_policy must be ABANDON or DELETE."
  }
}

variable "secret_ids" {
  description = "Secret container IDs to create. Values remain empty until set outside Terraform."
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

variable "artifact_repository_id" {
  description = "Artifact Registry Docker repository ID for reviewed images."
  type        = string
  default     = "assessment-images"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,62}$", var.artifact_repository_id))
    error_message = "artifact_repository_id must be 3-63 lowercase letters, digits, or hyphens."
  }
}

variable "log_bucket_id" {
  description = "Dedicated Logging bucket for assessment application logs."
  type        = string
  default     = "assessment-app"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,62}$", var.log_bucket_id))
    error_message = "log_bucket_id must be 3-63 lowercase letters, digits, or hyphens."
  }
}

variable "log_retention_days" {
  description = "Bounded retention for the dedicated application log bucket."
  type        = number
  default     = 30

  validation {
    condition     = floor(var.log_retention_days) == var.log_retention_days && var.log_retention_days >= 30 && var.log_retention_days <= 365
    error_message = "log_retention_days must be a whole number from 30 to 365."
  }
}

variable "budget_amount_units" {
  description = "Monthly budget ceiling in whole currency units. Set to 0 to skip budget resources."
  type        = number
  default     = 10

  validation {
    condition     = floor(var.budget_amount_units) == var.budget_amount_units && var.budget_amount_units >= 0
    error_message = "budget_amount_units must be a non-negative whole number."
  }
}

variable "budget_currency_code" {
  description = "ISO-4217 currency code used by the Cloud Billing budget."
  type        = string
  default     = "EUR"

  validation {
    condition     = can(regex("^[A-Z]{3}$", var.budget_currency_code))
    error_message = "budget_currency_code must be a three-letter uppercase ISO-4217 code."
  }
}

variable "budget_alert_thresholds" {
  description = "Monotonic budget thresholds that trigger alert notifications."
  type        = list(number)
  default     = [0.5, 0.9, 1.0]

  validation {
    condition = (
      length(var.budget_alert_thresholds) > 0 &&
      alltrue([
        for threshold in var.budget_alert_thresholds :
        threshold > 0 && threshold <= 1
      ]) &&
      alltrue([
        for index, threshold in var.budget_alert_thresholds :
        index == 0 ? true : threshold > var.budget_alert_thresholds[index - 1]
      ])
    )
    error_message = "budget_alert_thresholds must be strictly increasing values between 0 and 1."
  }
}

variable "budget_notification_emails" {
  description = "Budget alert recipients. At least one is required before budget resources are enabled."
  type        = set(string)
  default     = []

  validation {
    condition = alltrue([
      for address in var.budget_notification_emails :
      can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", address))
    ])
    error_message = "budget_notification_emails must contain plain email addresses."
  }
}

variable "billing_account_id" {
  description = "Billing account ID used for Cloud Billing budgets. Empty disables budget creation."
  type        = string
  default     = ""

  validation {
    condition = var.billing_account_id == "" || can(
      regex("^[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$", var.billing_account_id)
    )
    error_message = "billing_account_id must be empty or look like 000000-000000-000000."
  }
}

variable "api_service_account_email" {
  description = "Runtime API service-account email from the bootstrap stack."
  type        = string

  validation {
    condition = (
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.api_service_account_email)) &&
      endswith(var.api_service_account_email, "@${var.project_id}.iam.gserviceaccount.com")
    )
    error_message = "api_service_account_email must be a GCP service-account email."
  }
}

variable "worker_service_account_email" {
  description = "Runtime worker service-account email from the bootstrap stack."
  type        = string

  validation {
    condition = (
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.worker_service_account_email)) &&
      endswith(var.worker_service_account_email, "@${var.project_id}.iam.gserviceaccount.com")
    )
    error_message = "worker_service_account_email must be a GCP service-account email."
  }
}

variable "deployer_service_account_email" {
  description = "Terraform deployer service-account email from the bootstrap stack."
  type        = string

  validation {
    condition = (
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.deployer_service_account_email)) &&
      endswith(var.deployer_service_account_email, "@${var.project_id}.iam.gserviceaccount.com") &&
      length(toset([
        var.api_service_account_email,
        var.worker_service_account_email,
        var.deployer_service_account_email,
      ])) == 3
    )
    error_message = "deployer_service_account_email must belong to the project and all workload identities must be distinct."
  }
}
