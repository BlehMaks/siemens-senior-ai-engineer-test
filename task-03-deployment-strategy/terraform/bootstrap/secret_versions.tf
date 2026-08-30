resource "random_password" "secret_version" {
  for_each = var.seed_secret_versions ? var.secret_ids : {}

  length  = 64
  special = false
}

resource "google_secret_manager_secret_version" "initial" {
  for_each = var.seed_secret_versions ? var.secret_ids : {}

  secret      = google_secret_manager_secret.managed[each.key].id
  secret_data = random_password.secret_version[each.key].result
}
