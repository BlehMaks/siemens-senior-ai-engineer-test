resource "terraform_data" "secret_version" {
  for_each = var.seed_secret_versions ? var.secret_ids : {}

  triggers_replace = [google_secret_manager_secret.managed[each.key].id]

  provisioner "local-exec" {
    command = "${path.module}/../../scripts/seed_secret_version.sh"

    environment = {
      GCP_PROJECT_ID = var.project_id
      GCP_SECRET_ID  = google_secret_manager_secret.managed[each.key].secret_id
    }
  }
}
