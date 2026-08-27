variable "mode" {
  description = "Budget baseline or hardened load-balancer ingress posture."
  type        = string
  default     = "baseline"

  validation {
    condition     = contains(["baseline", "hardened"], var.mode)
    error_message = "mode must be baseline or hardened."
  }
}
