# Cloud resource and IAM manifest

This is the up-front change list for the Tasks 1 to 3 deployment. It describes
the default `sai/dev` cell in `europe-west3`. With the supplied project, replace
`PROJECT_ID` with `liquidity-planning-platform` and `PROJECT_NUMBER` with
`1027058459333`.

The wrapper does not create any of these resources with ad-hoc `gcloud`
commands. It supplies inputs to the three Terraform roots and uses `gcloud` only
for discovery, secret payload transport, and verification.

## Access needed before the first run

The Google Cloud project, its billing link, the GitHub repository, and the human
accounts already have to exist. Terraform does not grant privileges to the
operator running it.

The Google Cloud operator needs permissions equivalent to these predefined
roles on the project:

- `roles/serviceusage.serviceUsageAdmin`;
- `roles/storage.admin`;
- `roles/iam.serviceAccountAdmin`;
- `roles/iam.workloadIdentityPoolAdmin`;
- `roles/resourcemanager.projectIamAdmin`;
- `roles/iam.roleAdmin`;
- `roles/secretmanager.admin`;
- `roles/cloudtasks.queueAdmin`;
- `roles/run.admin`.

On the linked billing account, the operator needs `roles/billing.admin` so that
Terraform can grant the deployer its narrower budget-management role. On
GitHub, the authenticated account needs repository administration permission to
create the protected Environment, its reviewer rule, deployment branch policy,
and Actions variables. An organization may replace any predefined role with a
custom role containing the same permissions.

The post-deploy smoke uses the operator's Application Default Credentials. That
identity needs `roles/run.viewer` on the project,
`roles/secretmanager.secretAccessor` on `sai-dev-api-key-pepper`, and
`roles/datastore.user` restricted to
`projects/PROJECT_ID/databases/sai-dev`. The infrastructure apply does not use
these three grants. An existing project owner already has the required
permissions; a separate smoke operator should receive only this access.

The linked billing account must use EUR. The wrapper reads `currencyCode` before
the first Terraform mutation and stops if it is not `EUR`, because the test
budget is fixed at EUR 5. The budget recipient must be a monitored human mailbox
with a complete domain; service-account addresses are rejected.

## Google Cloud entities

| Kind | Name or count | Owner and purpose |
|---|---|---|
| Enabled APIs | 14 services listed below | Shared project capabilities; Terraform leaves them enabled on destroy |
| Terraform state buckets | `PROJECT_ID-sai-bootstrap-tf-state`, `PROJECT_ID-sai-app-tf-state` | Privileged bootstrap state and application delivery state are separated |
| Service accounts | `sai-dev-api`, `sai-dev-worker`, `sai-dev-tasks`, `sai-dev-deploy` | API runtime, worker runtime, Cloud Tasks OIDC caller, and CI deployer |
| Google-managed service identity | Cloud Tasks service agent for `PROJECT_NUMBER` | Mints the queue delivery OIDC token |
| Workload Identity pool/provider | `sai-dev-github` / `github-actions` | Accepts only the configured repository ID, `master`, and `gcp-dev` Environment |
| Secret Manager secrets | `sai-dev-api-key-pepper`, `sai-dev-task-signing-hmac` | Random initial versions are added only when no enabled version exists |
| Cloud Tasks queue | `sai-dev-run-dispatch` | One dispatch per second, one concurrent delivery, bounded retries |
| Firestore database | `sai-dev` in `europe-west3` | Named Native-mode database with deletion protection, PITR, and `ABANDON` deletion policy |
| Firestore composite indexes | `sessions`, `runs`, `run_events`, `audit_entries`, `quota_execution_leases`, `quota_sse_leases` | Query paths required by the API, shared security state, and quotas |
| Artifact Registry | `assessment-images` | Immutable Docker tags and digest-pinned deployment |
| Cloud Run services | `sai-dev-api`, `sai-dev-worker` | Both scale to zero and are capped at one instance in the test cell |
| Logging bucket | `assessment-app` | Regional application logs with 30-day retention |
| Monitoring channel | One channel per configured email | The wrapper defaults to the active human `gcloud` account |
| Billing budget | `sai-dev-assessment-budget` | EUR 5 project budget with 20%, 50%, 80%, and 100% alerts |
| Project custom role | `sai_dev_terraform_deployer` | Database and named Cloud Run lifecycle operations not covered by the selected predefined roles |
| GitHub branch protection | `master` | Rejects deletion, force pushes, and non-linear history, including for administrators |

The enabled services are:

```text
artifactregistry.googleapis.com
billingbudgets.googleapis.com
cloudresourcemanager.googleapis.com
cloudtasks.googleapis.com
firestore.googleapis.com
iam.googleapis.com
iamcredentials.googleapis.com
logging.googleapis.com
monitoring.googleapis.com
run.googleapis.com
secretmanager.googleapis.com
serviceusage.googleapis.com
storage.googleapis.com
sts.googleapis.com
```

The EUR 5 budget is an alerting boundary, not a payment hard stop. The actual
spend guard comes from scale-to-zero, one-instance maxima, the queue rate limit,
short retention, and a small smoke workload. The bootstrap refuses to report a
successful deployment if the budget or instance limits are missing from the
applied Terraform state.

## IAM grants made by Terraform

| Principal | Scope | Role or permissions |
|---|---|---|
| API service account | `sai-dev` Firestore database condition | `roles/datastore.user` |
| Worker service account | `sai-dev` Firestore database condition | `roles/datastore.user` |
| API and worker service accounts | Each of the two secrets | `roles/secretmanager.secretAccessor` |
| API service account | Dispatch queue | `roles/cloudtasks.enqueuer`, `roles/cloudtasks.taskDeleter`, `roles/cloudtasks.viewer` |
| Worker service account | Dispatch queue | `roles/cloudtasks.taskDeleter`, `roles/cloudtasks.viewer` |
| Tasks service account | Worker Cloud Run service | `roles/run.invoker` |
| Cloud Tasks service agent | Tasks service account | `roles/iam.serviceAccountTokenCreator` |
| GitHub repository principal | Deployer service account | `roles/iam.workloadIdentityUser` |
| Deployer service account | Project | `roles/artifactregistry.admin`, `roles/logging.configWriter`, `roles/monitoring.notificationChannelEditor`, `roles/serviceusage.serviceUsageAdmin`, and the custom role below |
| Deployer service account | `sai-dev` Firestore database condition | `roles/datastore.indexAdmin` |
| Deployer service account | API and worker service accounts | `roles/iam.serviceAccountUser` |
| Deployer service account | Application state bucket | `roles/storage.objectAdmin` |
| Deployer service account | Artifact Registry repository | `roles/artifactregistry.writer` |
| Deployer service account | Linked billing account | `roles/billing.costsManager` |
| `allUsers` | API Cloud Run service | `roles/run.invoker` in the baseline public-API profile |

The deployer custom role contains exactly these permissions:

```text
datastore.databases.create
datastore.databases.getMetadata
datastore.databases.list
datastore.databases.update
datastore.locations.get
datastore.locations.list
datastore.operations.get
datastore.operations.list
resourcemanager.projects.get
run.operations.get
run.services.create
run.services.delete
run.services.get
run.services.update
```

The deployer has no database-delete permission, no access to the bootstrap state
bucket, no secret payload access, no queue-administration role, no project IAM
administration, and no service-account key. The named database and conditional
data/index bindings keep the assessment identities out of any pre-existing
`(default)` database used by another service in the same project.
Before the protected deployment plan, the migration script checks every known
index address before it changes state. It forgets an address only when it belongs
to this project and the `(default)` database; the cloud index itself is left
alone. An existing `sai-dev` address stays where it is. If an earlier version used
an `assessment_*` address for the same `sai-dev` index, Terraform moves that state
entry back to its stable address without recreating the index. Any other project,
database, or duplicate owner stops the deployment before the first change.
Matching Terraform `moved` declarations make a direct plan safe for
`assessment_*` state as well. The index resources use `prevent_destroy`, so a
direct plan against an old `(default)` address fails instead of replacing it.

## GitHub entities

Terraform protects `master`, creates the `gcp-dev` repository Environment,
restricts deployments to that branch, and assigns the requested reviewer. It
writes these Environment variables:

```text
GCP_API_SERVICE_ACCOUNT
GCP_BILLING_ACCOUNT_ID
GCP_BUDGET_NOTIFICATION_EMAILS
GCP_DEPLOYER_SERVICE_ACCOUNT
GCP_PROJECT_ID
GCP_PROJECT_NUMBER
GCP_REGION
GCP_SECRET_IDS
GCP_TASKS_SERVICE_ACCOUNT
GCP_TERRAFORM_STATE_BUCKET
GCP_WORKER_SERVICE_ACCOUNT
GCP_WORKLOAD_IDENTITY_PROVIDER
```

These are identifiers, not credentials. GitHub receives no service-account key
and no Secret Manager payload.
