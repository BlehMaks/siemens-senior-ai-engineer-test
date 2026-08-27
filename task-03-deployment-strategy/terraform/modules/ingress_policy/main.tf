locals {
  policy = {
    mode                        = var.mode
    api_ingress                 = var.mode == "hardened" ? "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" : "INGRESS_TRAFFIC_ALL"
    api_default_uri_disabled    = var.mode == "hardened"
    api_allow_unauthenticated   = true
    worker_ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    worker_default_uri_disabled = false
    requires_external_lb        = var.mode == "hardened"
    cloud_armor_expected        = var.mode == "hardened"
  }
}
