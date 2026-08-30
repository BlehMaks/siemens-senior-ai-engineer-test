# Bootstrap Terraform

The bootstrap prepares the delivery boundary for the Tasks 1 to 3 assessment
service. It is split into two Terraform roots to solve the first-run state problem:

1. `terraform/state_bucket` creates separate private, versioned GCS buckets for
   privileged bootstrap state and application delivery state;
2. `terraform/bootstrap` stores its own state in the privileged bucket and
   creates the remaining GCP and GitHub resources.

Operators should use `scripts/bootstrap.sh` rather than call these roots by hand.

## Managed resources

The main bootstrap creates or configures:

- required project APIs;
- separate API, worker, Cloud Tasks, and deployer service accounts;
- scoped predefined roles and small custom roles;
- database-conditioned runtime and index roles for the named `sai-dev` Firestore
  database, with no database-delete permission for the deployer;
- repository-ID, branch, and environment-bound GitHub WIF;
- `master` protection against deletion, force pushes, and non-linear history;
- the GitHub `gcp-dev` Environment, branch policy, reviewer, and Actions variables;
- regional, deletion-protected Secret Manager containers;
- one initial random version for each required secret when no enabled version
  exists;
- the authenticated Cloud Tasks queue and its OIDC caller policy;
- application-state access for the deployer, with no bootstrap-state access;
- a billing-account-scoped budget role for the EUR 5 dev alert.

The application root owns the Cloud Run services. Bootstrap owns their
post-deploy invoker policy and grants the deployer only the lifecycle operations
needed for the two deterministic service names. It does not grant project IAM
administration, queue lifecycle control, Secret Manager payload access, route
invocation, or Cloud Run SSH.

## Secret handling

Terraform generates both initial secret values and creates their Secret Manager
versions. The values are sensitive and remain in the protected, versioned
bootstrap state. They are absent from command arguments and GitHub variables.

## Operator commands

Terraform 1.9.8 is required. Set `TERRAFORM_BIN` when it is not on `PATH`:

```bash
export TERRAFORM_BIN=/absolute/path/to/terraform

./task-03-deployment-strategy/scripts/bootstrap.sh plan \
  PROJECT_ID OWNER/REPOSITORY REVIEWER REGION

./task-03-deployment-strategy/scripts/bootstrap.sh apply \
  PROJECT_ID OWNER/REPOSITORY REVIEWER REGION

./task-03-deployment-strategy/scripts/bootstrap.sh verify \
  PROJECT_ID OWNER/REPOSITORY REVIEWER REGION

./task-03-deployment-strategy/scripts/bootstrap.sh deploy \
  PROJECT_ID OWNER/REPOSITORY REVIEWER REGION
```

`plan` is read-only. On a first run it shows the two foundation buckets and stops.
`apply` creates and verifies the bootstrap. `verify` requires a no-drift plan and
checks the GitHub output and queue. `deploy` does the same work as `apply`, then
dispatches `deploy.yml` only when local `HEAD` equals remote `master`; after the
workflow it verifies the EUR 5 budget and one-instance Cloud Run caps from
application state. The dispatch carries that verified SHA, and the workflow
fails before cloud authentication if GitHub resolves `master` to another commit.

## Guarantees and limits

- no service-account keys;
- no primitive Owner, Editor, or Viewer grants;
- state bucket versioning, uniform access, public access prevention, and no force
  destroy;
- provider locks for Darwin ARM64, Darwin AMD64, and Linux AMD64;
- idempotent secret seeding and Terraform retry behavior;
- no direct resource creation by the wrapper.

The complete entity and IAM inventory is maintained in
[`docs/cloud-resource-manifest.md`](../../../docs/cloud-resource-manifest.md).

The project and billing relationship must already exist. A human credential is
needed for the first bootstrap, and GitHub may require that person to approve the
protected deployment. Account creation, billing setup, MFA, terms, and corporate
policy decisions are outside Terraform's authority. The billing account must use
EUR, and the alert recipient must be a monitored human mailbox with a complete
domain. The wrapper validates both before Terraform changes either platform.
