output "api_service" {
  description = "Bounded API Cloud Run service contract."
  value = {
    name                   = google_cloud_run_v2_service.api.name
    uri                    = google_cloud_run_v2_service.api.uri
    ingress                = google_cloud_run_v2_service.api.ingress
    default_uri_disabled   = google_cloud_run_v2_service.api.default_uri_disabled
    service_account        = google_cloud_run_v2_service.api.template[0].service_account
    max_instances          = google_cloud_run_v2_service.api.template[0].scaling[0].max_instance_count
    min_instances          = google_cloud_run_v2_service.api.template[0].scaling[0].min_instance_count
    concurrency            = google_cloud_run_v2_service.api.template[0].max_instance_request_concurrency
    timeout                = google_cloud_run_v2_service.api.template[0].timeout
    public_invoker_enabled = var.api_allow_unauthenticated
    image                  = local.image
  }
}

output "worker_service" {
  description = "Bounded worker Cloud Run service contract."
  value = {
    name            = google_cloud_run_v2_service.worker.name
    uri             = google_cloud_run_v2_service.worker.uri
    ingress         = google_cloud_run_v2_service.worker.ingress
    service_account = google_cloud_run_v2_service.worker.template[0].service_account
    max_instances   = google_cloud_run_v2_service.worker.template[0].scaling[0].max_instance_count
    min_instances   = google_cloud_run_v2_service.worker.template[0].scaling[0].min_instance_count
    concurrency     = google_cloud_run_v2_service.worker.template[0].max_instance_request_concurrency
    timeout         = google_cloud_run_v2_service.worker.template[0].timeout
    dispatch_path   = var.worker_dispatch_path
    invoker_member  = google_cloud_run_v2_service_iam_member.worker_invoker.member
    invoker_role    = google_cloud_run_v2_service_iam_member.worker_invoker.role
  }
}

output "dispatch_queue" {
  description = "Cloud Tasks queue contract for authenticated worker delivery."
  value = {
    name                      = google_cloud_tasks_queue.dispatch.name
    location                  = google_cloud_tasks_queue.dispatch.location
    max_dispatches_per_second = google_cloud_tasks_queue.dispatch.rate_limits[0].max_dispatches_per_second
    max_concurrent_dispatches = google_cloud_tasks_queue.dispatch.rate_limits[0].max_concurrent_dispatches
    max_attempts              = google_cloud_tasks_queue.dispatch.retry_config[0].max_attempts
    max_retry_duration        = google_cloud_tasks_queue.dispatch.retry_config[0].max_retry_duration
    min_backoff               = google_cloud_tasks_queue.dispatch.retry_config[0].min_backoff
    max_backoff               = google_cloud_tasks_queue.dispatch.retry_config[0].max_backoff
    oidc_service_account      = google_cloud_tasks_queue.dispatch.http_target[0].oidc_token[0].service_account_email
    oidc_audience             = google_cloud_tasks_queue.dispatch.http_target[0].oidc_token[0].audience
    target_host               = google_cloud_tasks_queue.dispatch.http_target[0].uri_override[0].host
    target_path               = google_cloud_tasks_queue.dispatch.http_target[0].uri_override[0].path_override[0].path
  }
}

output "iam_contract" {
  description = "Least-privilege invocation and enqueue permissions."
  value = {
    api_queue_enqueuer_role        = google_cloud_tasks_queue_iam_member.api_enqueuer.role
    api_queue_enqueuer_member      = google_cloud_tasks_queue_iam_member.api_enqueuer.member
    worker_invoker_member          = google_cloud_run_v2_service_iam_member.worker_invoker.member
    worker_invoker_role            = google_cloud_run_v2_service_iam_member.worker_invoker.role
    tasks_service_agent_member     = google_service_account_iam_member.tasks_service_agent_token_creator.member
    tasks_service_agent_token_role = google_service_account_iam_member.tasks_service_agent_token_creator.role
  }
}

output "required_services" {
  description = "APIs that the execution-plane module keeps enabled."
  value       = sort(tolist(local.required_services))
}
