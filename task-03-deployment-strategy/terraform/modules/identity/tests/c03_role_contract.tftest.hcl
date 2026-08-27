mock_provider "google" {}

variables {
  project_id   = "contract-assignment-dev"
  account_id   = "contract-account"
  display_name = "Contract account"
  description  = "Identity validation contract fixture."
}

run "safe_computed_role" {
  command = plan

  variables {
    project_roles = [join("", ["roles/", "storage.admin"])]
  }
}

run "computed_primitive_roles_are_rejected" {
  command = plan

  variables {
    project_roles = [join("", ["roles/", "EdItOr"])]
  }

  expect_failures = [var.project_roles]
}

run "role_boundary_whitespace_is_rejected" {
  command = plan

  variables {
    project_roles = ["roles/owner "]
  }

  expect_failures = [var.project_roles]
}
