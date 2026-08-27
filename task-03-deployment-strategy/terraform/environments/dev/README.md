# C04 managed services: dev environment

This stack is the first application-facing Terraform slice after bootstrap. It
creates only the low-cost managed resources that the assessment cell needs:
Firestore, Secret Manager containers, Artifact Registry, one dedicated Logging
bucket, an optional Cloud Billing budget, and the bounded C05A execution plane.

## What stays out of Terraform state

- secret payloads and versions;
- service-account keys;
- remote backend wiring to a live state bucket;
- any live plan or apply steps.

The stack accepts bootstrap service-account emails as plain inputs so validation
remains credential-free. Later reviewed automation can feed these values from
the C03 outputs without changing the resource contract.

## Default posture

- one explicit region: `europe-west3`;
- Firestore delete protection enabled and Terraform destroy set to `ABANDON`;
- Artifact Registry uses immutable tags;
- runtime identities get only `roles/datastore.user` and
  `roles/secretmanager.secretAccessor`;
- the deployer gets repository-scoped `roles/artifactregistry.writer`;
- secret containers and the log bucket stay in the configured region;
- the log bucket keeps 30 days of redacted application logs;
- the budget is optional, but when billing coordinates and an explicit recipient
  are present it defaults to EUR 10 and disables broad default-IAM recipients;
- the API and worker run as distinct Cloud Run services with separate service
  accounts and immutable image digests;
- Cloud Tasks uses a dedicated caller identity, bounded retry/rate settings,
  and OIDC delivery to the worker only;
- `ingress_mode = "baseline"` keeps the API on direct Cloud Run ingress for
  the cheapest path, while `"hardened"` disables the default URL and expects a
  later LB + Cloud Armor front door. Hardened mode retains the API's public IAM
  binding so the serverless NEG can reach it; network ingress and Task 2 API-key
  authentication remain the bypass and application-authentication controls.

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
