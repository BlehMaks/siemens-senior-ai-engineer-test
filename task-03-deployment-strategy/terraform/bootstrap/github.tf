data "github_repository" "target" {
  count = var.enable_github_wif ? 1 : 0

  full_name = var.github_repository
}

data "github_user" "reviewer" {
  count = var.enable_github_wif ? 1 : 0

  username = var.github_reviewer
}

resource "github_repository_environment" "deployment" {
  count = var.enable_github_wif ? 1 : 0

  repository          = data.github_repository.target[0].name
  environment         = var.github_environment
  prevent_self_review = false

  reviewers {
    users = [data.github_user.reviewer[0].id]
  }

  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "branch" {
  count = var.enable_github_wif ? 1 : 0

  repository     = data.github_repository.target[0].name
  environment    = github_repository_environment.deployment[0].environment
  branch_pattern = var.github_branch
}

resource "github_actions_environment_variable" "delivery" {
  for_each = var.enable_github_wif ? local.github_environment_variables : {}

  repository    = data.github_repository.target[0].name
  environment   = github_repository_environment.deployment[0].environment
  variable_name = each.key
  value         = each.value

  depends_on = [github_repository_environment_deployment_policy.branch]
}
