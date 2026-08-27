variable "project_id" {
  description = "Target GCP project for this service account."
  type        = string
}

variable "account_id" {
  description = "Service-account ID, not the email address."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}$", var.account_id))
    error_message = "account_id must satisfy GCP service-account naming rules."
  }
}

variable "display_name" {
  description = "Human-readable label for the service account."
  type        = string
}

variable "description" {
  description = "Short reason this identity exists."
  type        = string
}

variable "labels" {
  description = "Reserved for future label-aware modules. Included to keep the interface stable."
  type        = map(string)
  default     = {}
}

variable "project_roles" {
  description = "Non-primitive project roles required by this workload."
  type        = set(string)
  default     = []

  validation {
    condition = alltrue([
      for role in var.project_roles :
      role == trimspace(role) &&
      can(regex("^roles/[A-Za-z0-9_.]+$", role)) &&
      !contains(
        ["roles/owner", "roles/editor", "roles/viewer"],
        lower(role),
      )
    ])
    error_message = "project_roles must use non-primitive predefined roles."
  }
}

variable "workload_identity_members" {
  description = "Federated principals allowed to act as this service account."
  type        = map(string)
  default     = {}
}

variable "service_account_user_members" {
  description = "Members allowed to attach or impersonate this identity in reviewed deploy flows."
  type        = map(string)
  default     = {}
}

variable "token_creator_members" {
  description = "Members allowed to mint short-lived access tokens for this identity."
  type        = map(string)
  default     = {}
}
