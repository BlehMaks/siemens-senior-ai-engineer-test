output "model_plane" {
  description = "Deployable model-plane contract; null resource fields mean assessment mode."
  value = {
    profile                       = var.model_plane_profile
    enabled                       = local.enabled
    service_name                  = local.enabled ? google_cloud_run_v2_service.model[0].name : null
    service_uri                   = local.enabled ? google_cloud_run_v2_service.model[0].uri : null
    runtime_service_account_email = local.enabled ? google_service_account.model[0].email : null
    worker_invoker_member         = local.enabled ? "serviceAccount:${var.worker_service_account_email}" : null
    image                         = local.enabled ? var.model_image : null
    model_name                    = local.enabled ? var.model_name : null
    accelerator                   = local.enabled ? "nvidia-l4" : null
    min_instances                 = local.enabled ? var.model_min_instances : null
    max_instances                 = local.enabled ? var.model_max_instances : null
    public_invoker_count          = 0
  }
}
