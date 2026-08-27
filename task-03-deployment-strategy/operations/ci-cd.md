# Keyless delivery contract

The repository has three narrowly scoped workflows:

- `ci.yml` runs unprivileged Python, Terraform, image, vulnerability, and SBOM
  checks for pull requests and protected `master` updates;
- `infra-plan.yml` authenticates only after a manual dispatch on `master` and
  produces a one-day review artifact;
- `deploy.yml` rebuilds and scans `master`, transfers that exact local image to
  the protected job, promotes it to Artifact Registry, and applies Terraform by
  the resulting immutable digest.

Every third-party action is pinned to a full commit SHA. Repository permissions
default to none. Only the protected jobs receive `id-token: write`; neither
workflow accepts a service-account key or reads a GitHub secret.

## One-time setup

1. Apply `terraform/bootstrap` with a human-held administrator credential. Set
   `github_repository_id` to GitHub's immutable numeric repository ID,
   `github_repository` to its current `owner/repository`, `github_branch` to
   `master`, and `github_environment` to `gcp-dev`.
2. Create the GitHub environment `gcp-dev`, restrict it to `master`, and require
   a human reviewer. The WIF provider repeats these controls in its OIDC
   attribute condition; either boundary can deny federation.
3. Configure these environment variables from the reviewed bootstrap outputs:

   | Variable | Meaning |
   |---|---|
   | `GCP_PROJECT_ID` / `GCP_PROJECT_NUMBER` | Dedicated assessment project |
   | `GCP_REGION` | Single reviewed region, normally `europe-west3` |
   | `GCP_TERRAFORM_STATE_BUCKET` | Versioned C03 state bucket |
   | `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full WIF provider resource name |
   | `GCP_API_SERVICE_ACCOUNT` | API runtime identity |
   | `GCP_WORKER_SERVICE_ACCOUNT` | Worker runtime identity |
   | `GCP_TASKS_SERVICE_ACCOUNT` | Cloud Tasks OIDC caller |
   | `GCP_DEPLOYER_SERVICE_ACCOUNT` | Reviewed Terraform/deployment identity |
   | `GCP_BILLING_ACCOUNT_ID` | Optional budget billing account |
   | `GCP_BUDGET_NOTIFICATION_EMAILS` | Optional Terraform set, for example `["owner@example.com"]` |

4. Add enabled values to the two Terraform-owned Secret Manager containers,
   `sai-dev-api-key-pepper` and `sai-dev-task-signing-hmac`, through an
   out-of-band administrator session. Terraform and GitHub never receive the
   payloads. Deployment fails before Cloud Run creation if either container has
   no enabled version.

The budget remains disabled unless billing account, project number, positive
amount, and at least one notification address are all present. A deployer also
needs `roles/billing.costsManager` on that billing account when the budget is
enabled; this external scope cannot be granted by project Terraform.

## Review and deployment flow

Pull requests run only `ci.yml`, which has no OIDC permission and therefore
cannot obtain GCP credentials even when opened from a fork. A merge to `master`
reruns the same checks. An operator then dispatches `infra-plan.yml` with an OCI
`sha256` digest and reviews the short-lived plan artifact.

For deployment, dispatch `deploy.yml` on `master`. The unprivileged job creates
the image and SBOM. The `gcp-dev` approval gate protects the second job, which:

1. creates only the Terraform-managed foundation when the state is empty;
2. verifies out-of-band secret versions without reading their payloads;
3. pushes the exact tested tar artifact and resolves its registry digest;
4. saves a Terraform plan and applies only that plan;
5. prints the ready revision of each state-owned service.

Deployment concurrency does not cancel an in-flight apply. CI and plan runs do
cancel stale work. The GCS backend lock is the final serialization boundary.

## Dry verification and failure behavior

Before enabling the environment, run:

```bash
actionlint .github/workflows/*.yml
uv sync --frozen --all-packages --dev
uv run ruff check .
uv run pytest -q
terraform -chdir=task-03-deployment-strategy/terraform fmt -check -recursive
terraform -chdir=task-03-deployment-strategy/terraform/environments/dev init -backend=false
terraform -chdir=task-03-deployment-strategy/terraform/environments/dev validate
```

The workflows stop on a non-`master` ref, missing environment input, malformed
digest, failed tests, HIGH/CRITICAL image finding, unavailable remote state,
missing secret version, failed plan, or failed apply. They do not fall back to
static credentials, mutable image tags, local Terraform state, or a rebuild of
an older revision.
