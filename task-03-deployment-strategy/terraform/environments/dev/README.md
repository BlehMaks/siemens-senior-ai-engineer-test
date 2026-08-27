# C04 managed services: dev environment

This stack is the first application-facing Terraform slice after bootstrap. It
creates only the low-cost managed resources that the assessment cell needs:
Firestore, Secret Manager containers, Artifact Registry, one dedicated Logging
bucket, and an optional Cloud Billing budget.

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
  are present it defaults to EUR 10 and disables broad default-IAM recipients.

This slice prepares the regional log bucket. C05 owns the reviewed Cloud Run log
routing, so the bucket intentionally receives no application logs before that wiring
exists.

## Validation commands

```bash
terraform -chdir=terraform/modules/managed_services fmt -check
terraform -chdir=terraform/modules/managed_services init -backend=false
terraform -chdir=terraform/modules/managed_services validate
terraform -chdir=terraform/modules/managed_services test
terraform -chdir=terraform/environments/dev fmt -check
terraform -chdir=terraform/environments/dev init -backend=false
terraform -chdir=terraform/environments/dev validate
```
