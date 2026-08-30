variable "project_id" {
  description = "Production cell project ID."
  type        = string
}

variable "region" {
  description = "Approved Cloud Run GPU region."
  type        = string
}

variable "environment" {
  description = "staging or prod."
  type        = string
  default     = "prod"
}

variable "system_code" {
  description = "Short resource prefix."
  type        = string
  default     = "sai"
}

variable "model_plane_profile" {
  description = "assessment or cloud_run_gpu."
  type        = string
  default     = "assessment"
}

variable "model_image" {
  description = "Immutable model-serving image."
  type        = string
  default     = ""
}

variable "model_name" {
  description = "Approved immutable model identifier."
  type        = string
  default     = ""
}

variable "worker_service_account_email" {
  description = "Agent worker identity allowed to invoke the model."
  type        = string
  default     = ""
}

variable "deployer_service_account_email" {
  description = "Terraform identity allowed to attach the model runtime identity."
  type        = string
  default     = ""
}

variable "model_min_instances" {
  description = "Warm GPU floor."
  type        = number
  default     = 0
}

variable "model_max_instances" {
  description = "Hard GPU ceiling."
  type        = number
  default     = 1
}

variable "gpu_zonal_redundancy_disabled" {
  description = "Whether GPU zonal redundancy is explicitly disabled."
  type        = bool
  default     = false
}

variable "labels" {
  description = "Additional production labels."
  type        = map(string)
  default = {
    owner = "ml-platform"
  }
}
