# Keyless delivery contract

The repository has three narrowly scoped workflows:

- `ci.yml` runs unprivileged Python, Terraform, image, vulnerability, and SBOM
  checks for pull requests and protected `master` updates;
- `infra-plan.yml` authenticates only after a manual dispatch on `master` and
  produces a one-day review artifact;
- `deploy.yml` rebuilds and scans `master`, promotes that exact image, exposes a
  Terraform plan bound to its push digest, and applies only that artifact after
  a second protected-environment approval.

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
   | `GCP_CI_SERVICE_ACCOUNT` | WIF entry identity that delegates to the deployer |
   | `GCP_API_SERVICE_ACCOUNT` | API runtime identity |
   | `GCP_WORKER_SERVICE_ACCOUNT` | Worker runtime identity |
   | `GCP_TASKS_SERVICE_ACCOUNT` | Cloud Tasks OIDC caller |
   | `GCP_DEPLOYER_SERVICE_ACCOUNT` | Reviewed Terraform/deployment identity |
   | `GCP_SECRET_IDS` | JSON object with exactly `api_key_pepper` and `task_signing_hmac` container IDs |
   | `GCP_BILLING_ACCOUNT_ID` | Optional budget billing account |
   | `GCP_BUDGET_NOTIFICATION_EMAILS` | Optional Terraform set, for example `["owner@example.com"]` |

4. Add enabled values to the two Terraform-owned Secret Manager containers,
   `sai-dev-api-key-pepper` and `sai-dev-task-signing-hmac`, through an
   out-of-band administrator session. Terraform and GitHub never receive the
   payloads. The administrator must verify both enabled versions before the first
   application apply; the deployer intentionally cannot inspect Secret Manager.
5. Create the deterministic Cloud Run services and queue with one reviewed
   application apply. Reapply `terraform/bootstrap` as the human administrator
   with `enable_runtime_policy = true` and `api_allow_unauthenticated` matching
   the ingress mode. This one-time second phase installs runtime IAM after its
   targets exist. Keep these policies bootstrap-owned for later deployments.

The deployer deliberately holds Cloud Run and queue lifecycle permissions but no
service, queue, project, or service-account IAM-policy mutation permission. It
also has no route-invoke or SSH permission. Because deploying code under a
runtime service account is itself a privileged act, compromise of the approved
deploy job can still exercise that
runtime's permissions through replacement code. The two protected approvals,
exact WIF repository/ref/environment binding, immutable digest, and binary-plan
verification are the controls for that release authority.

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
the image and SBOM. A first `gcp-dev` approval admits the plan job, which:

1. creates only the Terraform-managed foundation when the state is empty;
2. pushes the exact tested tar artifact and resolves its registry digest;
3. publishes the binary plan, its readable rendering, and a manifest binding it
   to the workflow revision and push digest.

The apply job references `gcp-dev` again, so it requires a separate approval
after the plan artifact is available for review. It verifies the manifest,
downloads the artifact from the same workflow run, applies that exact binary
plan, and prints the ready revision of each state-owned service.

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
malformed secret-container IDs, failed plan, or failed apply. They do not fall
back to static credentials, mutable image tags, local Terraform state, or a
rebuild of an older revision.
