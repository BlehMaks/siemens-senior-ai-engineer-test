output "firestore" {
  description = "Assessment Firestore database contract."
  value = {
    name                    = google_firestore_database.assessment.name
    location                = google_firestore_database.assessment.location_id
    type                    = google_firestore_database.assessment.type
    concurrency_mode        = google_firestore_database.assessment.concurrency_mode
    delete_protection_state = google_firestore_database.assessment.delete_protection_state
    deletion_policy         = google_firestore_database.assessment.deletion_policy
    pitr                    = google_firestore_database.assessment.point_in_time_recovery_enablement
    composite_indexes = sort([
      google_firestore_index.audit_entries.collection,
      google_firestore_index.quota_execution_leases_active.collection,
      google_firestore_index.quota_sse_leases_active.collection,
      google_firestore_index.run_events.collection,
      google_firestore_index.runs.collection,
      google_firestore_index.sessions.collection,
    ])
  }
}

output "artifact_registry" {
  description = "Artifact Registry repository contract and least-privilege writer."
  value = {
    location       = google_artifact_registry_repository.containers.location
    repository_id  = google_artifact_registry_repository.containers.repository_id
    immutable_tags = google_artifact_registry_repository.containers.docker_config[0].immutable_tags
    writer_member  = google_artifact_registry_repository_iam_member.deployer_writer.member
    writer_role    = google_artifact_registry_repository_iam_member.deployer_writer.role
  }
}

output "logging" {
  description = "Dedicated log bucket contract."
  value = {
    bucket_id      = google_logging_project_bucket_config.application.bucket_id
    location       = google_logging_project_bucket_config.application.location
    retention_days = google_logging_project_bucket_config.application.retention_days
    description    = google_logging_project_bucket_config.application.description
  }
}

output "budget" {
  description = "Budget contract. Disabled until billing coordinates are supplied outside Terraform test fixtures."
  value = {
    enabled                     = local.budget_enabled
    display_name                = local.budget_enabled ? google_billing_budget.assessment[0].display_name : null
    amount_units                = local.budget_enabled ? google_billing_budget.assessment[0].amount[0].specified_amount[0].units : null
    currency_code               = local.budget_enabled ? google_billing_budget.assessment[0].amount[0].specified_amount[0].currency_code : null
    threshold_rules             = var.budget_alert_thresholds
    channel_count               = length(google_monitoring_notification_channel.budget_email)
    default_recipients_disabled = local.budget_enabled ? google_billing_budget.assessment[0].all_updates_rule[0].disable_default_iam_recipients : true
  }
}

output "required_services" {
  description = "APIs that this module expects to keep enabled."
  value       = sort(tolist(local.required_services))
}
