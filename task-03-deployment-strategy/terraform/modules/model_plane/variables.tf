variable "project_id" {
  description = "GCP project that hosts the regional model plane."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid lowercase GCP project identifier."
  }
}

variable "region" {
  description = "Approved Cloud Run GPU region."
  type        = string

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
    error_message = "region must be a concrete GCP region."
  }
}

variable "environment" {
  description = "Environment suffix used in resource names."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "The model plane is reserved for staging or prod."
  }
}

variable "system_code" {
  description = "Short resource prefix."
  type        = string
  default     = "sai"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,8}$", var.system_code))
    error_message = "system_code must be 2-9 lowercase characters, digits, or hyphens."
  }
}

variable "model_plane_profile" {
  description = "assessment creates nothing; cloud_run_gpu creates paid model-serving capacity."
  type        = string
  default     = "assessment"

  validation {
    condition     = contains(["assessment", "cloud_run_gpu"], var.model_plane_profile)
    error_message = "model_plane_profile must be assessment or cloud_run_gpu."
  }
}

variable "model_image" {
  description = "Immutable Ollama-compatible serving image with the approved model artifact baked in."
  type        = string
  default     = ""

  validation {
    condition = (
      var.model_plane_profile == "assessment" ? var.model_image == "" :
      can(regex("^[a-z0-9-]+-docker\\.pkg\\.dev/[a-z][a-z0-9-]{4,28}[a-z0-9]/[a-z0-9._-]+/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$", var.model_image)) &&
      startswith(var.model_image, "${var.region}-docker.pkg.dev/${var.project_id}/")
    )
    error_message = "cloud_run_gpu requires an Artifact Registry image pinned by sha256 digest; assessment requires an empty model_image."
  }
}

variable "model_name" {
  description = "Immutable model identifier exposed to the model gateway."
  type        = string
  default     = ""

  validation {
    condition = (
      var.model_plane_profile == "assessment" ? var.model_name == "" :
      can(regex("^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$", var.model_name)) &&
      !contains(["latest", "replace-me", "placeholder"], lower(var.model_name))
    )
    error_message = "cloud_run_gpu requires a concrete model_name; assessment requires an empty model_name."
  }
}

variable "worker_service_account_email" {
  description = "Only runtime identity allowed to invoke the model service."
  type        = string
  default     = ""

  validation {
    condition = (
      var.model_plane_profile == "assessment" ? var.worker_service_account_email == "" :
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.worker_service_account_email)) &&
      endswith(var.worker_service_account_email, "@${var.project_id}.iam.gserviceaccount.com")
    )
    error_message = "cloud_run_gpu requires a worker service-account email; assessment requires an empty value."
  }
}

variable "deployer_service_account_email" {
  description = "Terraform identity allowed to attach the dedicated model runtime identity."
  type        = string
  default     = ""

  validation {
    condition = (
      var.model_plane_profile == "assessment" ? var.deployer_service_account_email == "" :
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.deployer_service_account_email)) &&
      var.deployer_service_account_email != var.worker_service_account_email
    )
    error_message = "cloud_run_gpu requires a distinct deployer service-account email; assessment requires an empty value."
  }
}

variable "model_min_instances" {
  description = "Warm GPU instance floor. Zero preserves scale-to-zero."
  type        = number
  default     = 0

  validation {
    condition     = floor(var.model_min_instances) == var.model_min_instances && var.model_min_instances >= 0 && var.model_min_instances <= 2
    error_message = "model_min_instances must be a whole number from 0 through 2."
  }
}

variable "model_max_instances" {
  description = "Hard GPU instance cap."
  type        = number
  default     = 1

  validation {
    condition = (
      floor(var.model_max_instances) == var.model_max_instances &&
      var.model_max_instances >= 1 &&
      var.model_max_instances <= 5 &&
      var.model_max_instances >= var.model_min_instances
    )
    error_message = "model_max_instances must be a whole number from 1 through 5 and not below the minimum."
  }
}

variable "gpu_zonal_redundancy_disabled" {
  description = "Disable zonal redundancy only after a documented capacity and availability review."
  type        = bool
  default     = false
}

variable "labels" {
  description = "Additional labels that cannot override module-owned labels."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for key, value in var.labels :
      can(regex("^[a-z][a-z0-9_-]{0,62}$", key)) &&
      can(regex("^[a-z0-9_-]{1,63}$", value)) &&
      !contains(["component", "environment", "managed_by", "system"], key)
    ])
    error_message = "labels must be lowercase and cannot replace module-owned labels."
  }
}
