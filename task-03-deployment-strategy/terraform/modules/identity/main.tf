resource "google_service_account" "this" {
  project      = var.project_id
  account_id   = var.account_id
  display_name = var.display_name
  description  = var.description
}

resource "google_project_iam_member" "project_role" {
  for_each = var.project_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.this.email}"
}

resource "google_service_account_iam_member" "workload_identity_user" {
  for_each = var.workload_identity_members

  service_account_id = google_service_account.this.name
  role               = "roles/iam.workloadIdentityUser"
  member             = each.value
}

resource "google_service_account_iam_member" "service_account_user" {
  for_each = var.service_account_user_members

  service_account_id = google_service_account.this.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}

resource "google_service_account_iam_member" "token_creator" {
  for_each = var.token_creator_members

  service_account_id = google_service_account.this.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = each.value
}
