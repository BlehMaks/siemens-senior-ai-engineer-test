variable "project_id" {
  description = "GCP project ID that hosts the execution plane."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must satisfy GCP project ID naming rules."
  }
}

variable "project_number" {
  description = "Numeric GCP project number used for the Cloud Tasks service agent."
  type        = string

  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.project_number))
    error_message = "project_number must be a positive numeric project number."
  }
}

variable "region" {
  description = "Single assessment region."
  type        = string

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]$", var.region))
    error_message = "region must be a concrete GCP region such as europe-west3."
  }
}

variable "environment" {
  description = "Short environment name used in deterministic resource naming."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging, or prod."
  }
}

variable "system_code" {
  description = "Short lowercase system prefix reused across services and queues."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,14}$", var.system_code))
    error_message = "system_code must be a short lowercase resource prefix."
  }
}

variable "labels" {
  description = "Environment labels applied to execution resources."
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

variable "artifact_registry_location" {
  description = "Region that hosts the reviewed Artifact Registry repository."
  type        = string

  validation {
    condition     = var.artifact_registry_location == var.region
    error_message = "artifact_registry_location must match the execution region."
  }
}

variable "artifact_repository_id" {
  description = "Artifact Registry repository ID that holds the reviewed container image."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,62}$", var.artifact_repository_id))
    error_message = "artifact_repository_id must be a lowercase Artifact Registry ID."
  }
}

variable "image_name" {
  description = "Docker image name inside Artifact Registry."
  type        = string
  default     = "siemens-agent-api"

  validation {
    condition     = can(regex("^[a-z0-9]+(?:[._-][a-z0-9]+)*$", var.image_name))
    error_message = "image_name must be a lowercase OCI repository name."
  }
}

variable "image_digest" {
  description = "Immutable OCI image digest promoted into Cloud Run."
  type        = string

  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.image_digest))
    error_message = "image_digest must be a sha256:... OCI image digest."
  }
}

variable "api_service_account_email" {
  description = "API Cloud Run runtime identity."
  type        = string

  validation {
    condition = (
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.api_service_account_email)) &&
      endswith(var.api_service_account_email, "@${var.project_id}.iam.gserviceaccount.com")
    )
    error_message = "api_service_account_email must belong to project_id."
  }
}

variable "worker_service_account_email" {
  description = "Worker Cloud Run runtime identity."
  type        = string

  validation {
    condition = (
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.worker_service_account_email)) &&
      endswith(var.worker_service_account_email, "@${var.project_id}.iam.gserviceaccount.com") &&
      var.worker_service_account_email != var.api_service_account_email
    )
    error_message = "worker_service_account_email must belong to project_id and differ from the API identity."
  }
}

variable "tasks_service_account_email" {
  description = "Cloud Tasks OIDC caller identity."
  type        = string

  validation {
    condition = (
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.tasks_service_account_email)) &&
      endswith(var.tasks_service_account_email, "@${var.project_id}.iam.gserviceaccount.com") &&
      length(toset([
        var.api_service_account_email,
        var.worker_service_account_email,
        var.tasks_service_account_email,
      ])) == 3
    )
    error_message = "tasks_service_account_email must belong to project_id and all runtime identities must be distinct."
  }
}

variable "api_ingress" {
  description = "Resolved API ingress posture from the ingress policy module."
  type        = string

  validation {
    condition = contains([
      "INGRESS_TRAFFIC_ALL",
      "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
    ], var.api_ingress)
    error_message = "api_ingress must be the baseline or hardened policy value."
  }
}

variable "api_default_uri_disabled" {
  description = "Disable the default API URL when the hardened ingress mode is selected."
  type        = bool

  validation {
    condition = (
      var.api_default_uri_disabled ==
      (var.api_ingress == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER")
    )
    error_message = "api_default_uri_disabled must be true only for hardened ingress."
  }
}

variable "worker_ingress" {
  description = "Resolved worker ingress posture from the ingress policy module."
  type        = string

  validation {
    condition     = var.worker_ingress == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    error_message = "worker_ingress must allow only internal and load-balancer traffic."
  }
}

variable "worker_dispatch_path" {
  description = "Reserved worker HTTP path for Cloud Tasks delivery. C05B must implement this handler."
  type        = string
  default     = "/internal/tasks/run-delivery"

  validation {
    condition     = can(regex("^/[a-z0-9/_-]+$", var.worker_dispatch_path))
    error_message = "worker_dispatch_path must be an absolute lowercase path."
  }
}

variable "firestore_database_name" {
  description = "Firestore database name exposed to later application wiring."
  type        = string
  default     = "(default)"
}

variable "api_key_pepper_secret_id" {
  description = "Secret Manager secret ID that stores the API key pepper."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,254}$", var.api_key_pepper_secret_id))
    error_message = "api_key_pepper_secret_id must satisfy Secret Manager naming rules."
  }
}

variable "task_signing_hmac_secret_id" {
  description = "Secret Manager secret ID reserved for signed Cloud Tasks payload verification."
  type        = string

  validation {
    condition = (
      can(regex("^[a-z][a-z0-9-]{2,254}$", var.task_signing_hmac_secret_id)) &&
      var.task_signing_hmac_secret_id != var.api_key_pepper_secret_id
    )
    error_message = "task_signing_hmac_secret_id must be valid and differ from the API pepper secret."
  }
}

variable "api_timeout_seconds" {
  description = "API request timeout in seconds."
  type        = number
  default     = 60

  validation {
    condition     = floor(var.api_timeout_seconds) == var.api_timeout_seconds && var.api_timeout_seconds >= 10 && var.api_timeout_seconds <= 300
    error_message = "api_timeout_seconds must be a whole number from 10 to 300."
  }
}

variable "worker_timeout_seconds" {
  description = "Worker request timeout in seconds."
  type        = number
  default     = 300

  validation {
    condition     = floor(var.worker_timeout_seconds) == var.worker_timeout_seconds && var.worker_timeout_seconds >= 30 && var.worker_timeout_seconds <= 1800
    error_message = "worker_timeout_seconds must be a whole number from 30 to 1800."
  }
}

variable "shutdown_seconds" {
  description = "Bounded graceful shutdown window passed to both services."
  type        = number
  default     = 10

  validation {
    condition     = floor(var.shutdown_seconds) == var.shutdown_seconds && var.shutdown_seconds >= 1 && var.shutdown_seconds <= 30
    error_message = "shutdown_seconds must be a whole number from 1 to 30."
  }
}

variable "api_max_instances" {
  description = "Maximum API replicas."
  type        = number
  default     = 3

  validation {
    condition     = floor(var.api_max_instances) == var.api_max_instances && var.api_max_instances >= 1 && var.api_max_instances <= 20
    error_message = "api_max_instances must be a whole number from 1 to 20."
  }
}

variable "worker_max_instances" {
  description = "Maximum worker replicas."
  type        = number
  default     = 5

  validation {
    condition     = floor(var.worker_max_instances) == var.worker_max_instances && var.worker_max_instances >= 1 && var.worker_max_instances <= 50
    error_message = "worker_max_instances must be a whole number from 1 to 50."
  }
}

variable "api_concurrency" {
  description = "Maximum concurrent requests per API instance."
  type        = number
  default     = 20

  validation {
    condition     = floor(var.api_concurrency) == var.api_concurrency && var.api_concurrency >= 1 && var.api_concurrency <= 1000
    error_message = "api_concurrency must be a whole number from 1 to 1000."
  }
}

variable "worker_concurrency" {
  description = "Maximum concurrent requests per worker instance."
  type        = number
  default     = 1

  validation {
    condition     = floor(var.worker_concurrency) == var.worker_concurrency && var.worker_concurrency >= 1 && var.worker_concurrency <= 1000
    error_message = "worker_concurrency must be a whole number from 1 to 1000."
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
    condition     = floor(var.queue_max_retry_seconds) == var.queue_max_retry_seconds && var.queue_max_retry_seconds >= 0 && var.queue_max_retry_seconds <= 3600
    error_message = "queue_max_retry_seconds must be a whole number from 0 to 3600."
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
