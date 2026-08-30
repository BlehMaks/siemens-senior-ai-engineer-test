# C04 managed services: dev environment

This stack is the first application-facing Terraform slice after bootstrap. It
creates only the low-cost application resources that the assessment cell needs:
Firestore, Artifact Registry, one dedicated Logging bucket, an optional Cloud
Billing budget, and the bounded C05A execution plane. Secret containers and their
runtime access policies remain in the human-held bootstrap stack.

## What stays out of Terraform state

- secret payloads and versions;
- service-account keys;
- any live plan or apply steps.

The stack accepts bootstrap service-account emails and secret IDs as plain inputs
so validation remains credential-free. Later reviewed automation can feed these
values from the C03 outputs without changing the resource contract.

The empty GCS backend block is intentional: local validation uses
`terraform init -backend=false`, while protected automation supplies the bucket
and `assessment/dev` prefix created by C03. No backend coordinate or credential
is committed.

## Default posture

- one explicit region: `europe-west3`;
- the named `sai-dev` Firestore database uses delete protection and Terraform
  destroy set to `ABANDON`, leaving any project `(default)` database untouched;
- each Firestore composite index uses `prevent_destroy`; the protected deploy
  workflow runs `scripts/migrate_firestore_index_state.sh` before planning so an
  old `(default)` state entry is forgotten without deleting its cloud index, while
  an existing `sai-dev` entry is preserved;
- Artifact Registry uses immutable tags;
- bootstrap grants database-conditioned `roles/datastore.user` and
  resource-scoped `roles/secretmanager.secretAccessor` only to the API and worker
  identities;
- bootstrap also owns the queue and Cloud Run invoker policies after these
  deterministic application services have been created; this stack exposes the
  expected queue and IAM contracts but never edits those resources or policies;
- the deployer gets repository-scoped `roles/artifactregistry.writer`;
- secret containers and the log bucket stay in the configured region;
- the log bucket keeps 30 days of redacted application logs;
- the wrapper supplies the linked billing account and a monitored recipient; the
  dev root creates a EUR 5 budget, alerts at 20%, 50%, 80%, and 100%, and disables
  broad default-IAM recipients;
- API and worker both scale to zero and are capped at one instance;
- the API and worker run as distinct Cloud Run services with separate service
  accounts and immutable image digests;
- Cloud Tasks uses a dedicated caller identity, bounded retry/rate settings,
  and OIDC delivery to the worker only;
- `ingress_mode = "baseline"` keeps the API on direct Cloud Run ingress for
  the cheapest path, while `"hardened"` disables the default URL and expects a
  later authenticated LB + Cloud Armor front door. Hardened mode removes the
  API's public IAM binding so unauthenticated callers cannot invoke Cloud Run
  directly.

This slice prepares the regional log bucket. C05 owns the reviewed Cloud Run log
routing, so the bucket intentionally receives no application logs before that wiring
exists.

## C05A notes

- The current Task 2 application does not yet expose the reserved worker
  dispatch path `/internal/tasks/run-delivery`; `C05B/C05C` must implement the
  handler and connect it to the queue contract produced here.
- Worker ingress is never public in this stack.
- The worker default URL stays enabled because same-project Cloud Tasks uses it;
  ingress and OIDC IAM still reject unauthenticated delivery.
- The attack-path review for the two ingress modes is in
  [c05a-attack-path.md](c05a-attack-path.md).

## Validation commands

```bash
terraform -chdir=terraform/modules/managed_services fmt -check
terraform -chdir=terraform/modules/managed_services init -backend=false
terraform -chdir=terraform/modules/managed_services validate
terraform -chdir=terraform/modules/managed_services test
terraform -chdir=terraform/modules/ingress_policy test
terraform -chdir=terraform/modules/run_services test
terraform -chdir=terraform/environments/dev fmt -check
terraform -chdir=terraform/environments/dev init -backend=false
terraform -chdir=terraform/environments/dev validate
```
