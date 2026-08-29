output "api_service" {
  description = "Bounded API Cloud Run service contract."
  value = {
    name                    = google_cloud_run_v2_service.api.name
    uri                     = google_cloud_run_v2_service.api.uri
    ingress                 = google_cloud_run_v2_service.api.ingress
    default_uri_disabled    = google_cloud_run_v2_service.api.default_uri_disabled
    service_account         = google_cloud_run_v2_service.api.template[0].service_account
    max_instances           = google_cloud_run_v2_service.api.template[0].scaling[0].max_instance_count
    min_instances           = google_cloud_run_v2_service.api.template[0].scaling[0].min_instance_count
    concurrency             = google_cloud_run_v2_service.api.template[0].max_instance_request_concurrency
    timeout                 = google_cloud_run_v2_service.api.template[0].timeout
    public_invoker_required = var.api_allow_unauthenticated
    image                   = local.image
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
    invoker_members = ["serviceAccount:${var.tasks_service_account_email}"]
    invoker_role    = "roles/run.invoker"
  }
}

output "dispatch_queue" {
  description = "Deterministic contract for the bootstrap-owned Cloud Tasks queue."
  value = {
    name      = local.dispatch_queue_name
    path      = local.dispatch_queue_path
    location  = var.region
    ownership = "bootstrap"
  }
}

output "iam_contract" {
  description = "Least-privilege invocation and queue policy shared by bootstrap and application stacks."
  value = {
    api_queue_enqueuer_role        = "roles/cloudtasks.enqueuer"
    api_queue_enqueuer_member      = "serviceAccount:${var.api_service_account_email}"
    task_viewer_members            = sort(["serviceAccount:${var.api_service_account_email}", "serviceAccount:${var.worker_service_account_email}"])
    task_viewer_role               = "roles/cloudtasks.viewer"
    task_deleter_members           = sort(["serviceAccount:${var.api_service_account_email}", "serviceAccount:${var.worker_service_account_email}"])
    task_deleter_role              = "roles/cloudtasks.taskDeleter"
    worker_invoker_members         = ["serviceAccount:${var.tasks_service_account_email}"]
    worker_invoker_role            = "roles/run.invoker"
    worker_invoker_binding_count   = 1
    api_public_invoker_count       = length(google_cloud_run_v2_service_iam_member.api_public_invoker)
    tasks_service_agent_token_role = "roles/iam.serviceAccountTokenCreator"
  }
}

output "required_services" {
  description = "APIs that the execution-plane module keeps enabled."
  value       = sort(tolist(local.required_services))
}
