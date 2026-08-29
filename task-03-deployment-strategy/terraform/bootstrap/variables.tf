variable "project_id" {
  description = "GCP project ID that hosts the assessment cell."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid lowercase GCP project identifier."
  }
}

variable "region" {
  description = "Primary GCP region for the assessment cell."
  type        = string
  default     = "europe-west3"

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
    error_message = "region must look like a GCP region such as europe-west3."
  }
}

variable "environment" {
  description = "Short environment name used in deterministic identity naming."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of dev, staging, or prod."
  }
}

variable "system_code" {
  description = "Short lowercase prefix that keeps service-account IDs under GCP limits."
  type        = string
  default     = "sai"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,8}$", var.system_code))
    error_message = "system_code must be 2-9 lowercase characters, digits, or hyphens."
  }
}

variable "state_bucket_name" {
  description = "Name of the GCS bucket that later Terraform stacks use as a remote backend."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid GCS bucket name."
  }
}

variable "secret_ids" {
  description = "Bootstrap-owned Secret Manager container IDs."
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

variable "seed_secret_versions" {
  description = "Create an initial random version for an empty bootstrap-owned secret."
  type        = bool
  default     = true
}

variable "worker_dispatch_path" {
  description = "Reserved worker path used by the bootstrap-owned Cloud Tasks queue."
  type        = string
  default     = "/internal/tasks/run-delivery"

  validation {
    condition     = can(regex("^/[a-z0-9/_-]+$", var.worker_dispatch_path)) && !strcontains(var.worker_dispatch_path, "//")
    error_message = "worker_dispatch_path must be an absolute lowercase path without empty segments."
  }
}

variable "queue_max_dispatches_per_second" {
  description = "Cloud Tasks steady-state dispatch rate."
  type        = number
  default     = 1

  validation {
    condition     = var.queue_max_dispatches_per_second > 0 && var.queue_max_dispatches_per_second <= 20
    error_message = "queue_max_dispatches_per_second must be greater than 0 and at most 20."
  }
}

variable "queue_max_concurrent_dispatches" {
  description = "Cloud Tasks concurrent in-flight deliveries."
  type        = number
  default     = 1

  validation {
    condition     = floor(var.queue_max_concurrent_dispatches) == var.queue_max_concurrent_dispatches && var.queue_max_concurrent_dispatches >= 1 && var.queue_max_concurrent_dispatches <= 20
    error_message = "queue_max_concurrent_dispatches must be a whole number from 1 to 20."
  }
}

variable "queue_max_attempts" {
  description = "Cloud Tasks retry attempt cap, including the first delivery."
  type        = number
  default     = 5

  validation {
    condition     = floor(var.queue_max_attempts) == var.queue_max_attempts && var.queue_max_attempts >= 1 && var.queue_max_attempts <= 20
    error_message = "queue_max_attempts must be a whole number from 1 to 20."
  }
}

variable "queue_max_retry_seconds" {
  description = "Maximum retry window before Cloud Tasks gives up."
  type        = number
  default     = 900

  validation {
    condition     = floor(var.queue_max_retry_seconds) == var.queue_max_retry_seconds && var.queue_max_retry_seconds >= 1 && var.queue_max_retry_seconds <= 3600
    error_message = "queue_max_retry_seconds must be a whole number from 1 to 3600."
  }
}

variable "queue_min_backoff_seconds" {
  description = "Minimum Cloud Tasks retry backoff."
  type        = number
  default     = 5

  validation {
    condition     = floor(var.queue_min_backoff_seconds) == var.queue_min_backoff_seconds && var.queue_min_backoff_seconds >= 1 && var.queue_min_backoff_seconds <= 300
    error_message = "queue_min_backoff_seconds must be a whole number from 1 to 300."
  }
}

variable "queue_max_backoff_seconds" {
  description = "Maximum Cloud Tasks retry backoff."
  type        = number
  default     = 60

  validation {
    condition = (
      floor(var.queue_max_backoff_seconds) == var.queue_max_backoff_seconds &&
      var.queue_max_backoff_seconds >= var.queue_min_backoff_seconds &&
      var.queue_max_backoff_seconds <= 3600
    )
    error_message = "queue_max_backoff_seconds must be a whole number at least as large as min backoff and at most 3600."
  }
}

variable "labels" {
  description = "Small set of lowercase labels applied to bootstrap resources."
  type        = map(string)
  default = {
    owner = "platform"
  }

  validation {
    condition = alltrue([
      for key, value in var.labels :
      can(regex("^[a-z][a-z0-9_-]{0,62}$", key)) &&
      can(regex("^[a-z0-9_-]{1,63}$", value))
    ])
    error_message = "labels must use short lowercase label-compatible keys and values."
  }
}

variable "billing_account_id" {
  description = "Optional billing account ID copied into the protected GitHub environment."
  type        = string
  default     = ""

  validation {
    condition = (
      var.billing_account_id == "" ||
      can(regex("^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$", var.billing_account_id))
    )
    error_message = "billing_account_id must be empty or a canonical Cloud Billing account ID."
  }
}

variable "budget_notification_emails" {
  description = "Optional budget recipients copied into the protected GitHub environment."
  type        = set(string)
  default     = []

  validation {
    condition = alltrue([
      for address in var.budget_notification_emails :
      address == trimspace(address) && can(regex("^[^@[:space:]]+@[^@[:space:]]+$", address))
    ])
    error_message = "budget_notification_emails must contain trimmed email addresses."
  }
}

variable "enable_github_wif" {
  description = "Create the GitHub workload identity pool and provider when true."
  type        = bool
  default     = true
}

variable "github_repository" {
  description = "GitHub repository allowed to federate, in owner/repo form."
  type        = string
  default     = "example-org/siemens-senior-ai-engineer-test"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must use owner/repo syntax."
  }
}

variable "github_branch" {
  description = "Git branch allowed to federate through GitHub OIDC."
  type        = string
  default     = "master"

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+$", var.github_branch))
    error_message = "github_branch must be a plain branch name without spaces."
  }
}

variable "github_environment" {
  description = "Protected GitHub Actions environment that must match the OIDC token."
  type        = string
  default     = "gcp-dev"

  validation {
    condition = (
      !var.enable_github_wif ||
      can(regex("^[A-Za-z0-9_-]+$", var.github_environment))
    )
    error_message = "github_environment must be a short GitHub environment name when WIF is enabled."
  }
}

variable "github_reviewer" {
  description = "GitHub login required to approve protected deployment jobs."
  type        = string
  default     = ""

  validation {
    condition = (
      !var.enable_github_wif ||
      can(regex("^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$", var.github_reviewer))
    )
    error_message = "github_reviewer must be a valid GitHub login when WIF is enabled."
  }
}
