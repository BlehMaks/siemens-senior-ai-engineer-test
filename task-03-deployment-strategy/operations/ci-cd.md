# Keyless delivery contract

The repository uses three workflows:

- `ci.yml` runs unprivileged Python, Terraform, container, vulnerability, and SBOM
  checks on pull requests and updates to `master`;
- `infra-plan.yml` creates an approval-gated application plan for a supplied
  immutable image digest;
- `deploy.yml` rebuilds and scans `master`, pushes that exact image, exposes the
  Terraform plan, and applies only the reviewed artifact.

Every third-party action is pinned to a full commit SHA. Workflow permissions
start at none. Only protected jobs receive `id-token: write`, and no workflow uses
a service-account key.

## One-time setup

Run the Terraform wrapper from an authenticated operator computer:

```bash
./task-03-deployment-strategy/scripts/bootstrap.sh apply \
  PROJECT_ID OWNER/REPOSITORY REVIEWER REGION
```

Terraform configures both platforms. On GCP it creates the delivery identities,
WIF provider, scoped IAM, queue, and secret foundation. On GitHub it creates the
`gcp-dev` Environment, limits deployments to `master`, sets the reviewer, and
writes the environment variables consumed by the workflows.

The variables include project and region IDs, the application-state bucket, WIF
provider, deployer and runtime identities, secret container IDs, and required
budget inputs. Secret payloads never enter GitHub.

## Trust path

The short-lived identity exchange is:

```text
reviewed GitHub job
  -> repository ID + master + gcp-dev OIDC claims
deployer service account
  -> reviewed Terraform and Artifact Registry operations
```

The deployer can update the two named Cloud Run services and attach their runtime
identities. That is a high-trust release capability because replacement code runs
with those identities. Protected environment review, exact WIF claims, immutable
digests, and the binary-plan handoff are part of the security boundary.

## Deployment flow

Pull requests run `ci.yml` without cloud credentials. After `master` is pushed,
the operator uses:

```bash
./task-03-deployment-strategy/scripts/bootstrap.sh deploy \
  PROJECT_ID OWNER/REPOSITORY REVIEWER REGION
```

The wrapper verifies Terraform and dispatches `deploy.yml` with the exact remote
commit SHA and a random correlation ID. The first job checks GitHub's resolved
`master` against that SHA. A branch movement makes the correlated run fail before
it can obtain a cloud credential. The workflow then:

1. installs the locked workspace, runs application checks, builds the image,
   rejects HIGH or CRITICAL findings, and creates an SBOM;
2. enters the protected environment and obtains a short-lived GCP credential;
3. creates the managed foundation on an empty application state;
4. pushes the tested image and verifies the registry digest;
5. creates a binary Terraform plan and a manifest binding commit, digest, and run;
6. passes those artifacts to the apply job;
7. verifies provenance, applies only that plan, and prints the ready revisions.

Deployment concurrency never cancels an in-flight apply. CI and plan jobs may
cancel stale runs. The GCS state lock is the final serialization boundary.

## Failure behavior

The workflows stop on an unexpected branch, missing environment input, malformed
digest, failed code or Terraform checks, image finding, unavailable state,
malformed secret IDs, plan failure, or apply failure. They do not fall back to a
static credential, mutable image tag, local application state, or rebuild of an
older revision.

The bootstrap is safe to rerun after a partial failure. Terraform reuses remote
state, migrates the old combined backend when needed, keeps existing enabled
secret versions, and dispatches deployment only after verification succeeds.

The wrapper discovers the linked billing account and defaults the recipient to
the active human account. Terraform grants the deployer
`roles/billing.costsManager` on that account and creates the EUR 5 alert during
the application apply. Before any mutation, the wrapper also requires the
account currency to be EUR and rejects service-account or incomplete recipient
addresses. The operator must already have billing-account administration
permission for the first bootstrap.
