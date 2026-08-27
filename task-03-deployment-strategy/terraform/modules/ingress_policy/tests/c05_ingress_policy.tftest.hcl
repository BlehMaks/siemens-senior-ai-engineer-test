variables {
  mode = "baseline"
}

run "baseline_is_low_cost_and_public_only_for_api" {
  command = plan

  assert {
    condition     = output.policy.api_ingress == "INGRESS_TRAFFIC_ALL"
    error_message = "Baseline mode must keep the API on direct Cloud Run ingress."
  }

  assert {
    condition     = output.policy.api_allow_unauthenticated
    error_message = "Baseline mode should allow public API entry while app auth stays in Task 2."
  }

  assert {
    condition     = output.policy.worker_ingress == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    error_message = "Worker ingress must never be public."
  }
}

run "hardened_mode_disables_default_api_url_and_requires_lb" {
  command = plan

  variables {
    mode = "hardened"
  }

  assert {
    condition     = output.policy.api_default_uri_disabled
    error_message = "Hardened mode must disable the default Cloud Run URL."
  }

  assert {
    condition     = output.policy.requires_external_lb
    error_message = "Hardened mode must require an external load balancer path."
  }

  assert {
    condition     = output.policy.cloud_armor_expected
    error_message = "Hardened mode must expect Cloud Armor in front of the service."
  }

  assert {
    condition     = output.policy.api_allow_unauthenticated
    error_message = "The external load balancer must be able to reach the app-authenticated API backend."
  }
}
