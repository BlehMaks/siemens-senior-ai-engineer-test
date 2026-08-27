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

variable "github_repository_id" {
  description = "Immutable numeric GitHub repository ID allowed to federate."
  type        = string
  default     = ""

  validation {
    condition = (
      (!var.enable_github_wif && var.github_repository_id == "") ||
      can(regex("^[1-9][0-9]*$", var.github_repository_id))
    )
    error_message = "github_repository_id must be empty when WIF is disabled or a positive numeric GitHub repository ID."
  }
}

variable "github_branch" {
  description = "Git branch allowed to federate through GitHub OIDC."
  type        = string
  default     = "main"

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+$", var.github_branch))
    error_message = "github_branch must be a plain branch name without spaces."
  }
}

variable "github_environment" {
  description = "Optional GitHub Actions environment name that must match the OIDC token."
  type        = string
  default     = ""

  validation {
    condition = var.github_environment == "" || can(
      regex("^[A-Za-z0-9_-]+$", var.github_environment)
    )
    error_message = "github_environment must be empty or a short GitHub environment name."
  }
}
