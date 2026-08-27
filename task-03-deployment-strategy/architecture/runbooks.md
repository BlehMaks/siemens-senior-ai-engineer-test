# Assessment-cell operations runbooks

These commands cover the bounded `dev` assessment cell. They do not authorize a
paid deployment: use them only after the protected workflow has applied a reviewed
plan and the project budget is active. None of the examples prints API keys, secret
payloads, or credential files.

## Shared prerequisites

- `gcloud`, `jq`, `curl`, and Terraform 1.9.8 are installed as required by the
  selected command;
- the active identity is the reviewed operator/deployer identity, obtained through
  WIF or a short-lived administrator session rather than a service-account key;
- the exact project ID, numeric project number, `europe-west3` region, and `dev`
  environment have been copied from reviewed bootstrap outputs;
- a project-scoped billing budget is active before any smoke traffic;
- smoke keys belong to two different tenants. Key A needs session/run read and
  write scopes; key B needs session read scope.

The scripts reject non-`dev` targets. Normal preflight and smoke traffic should cost
well below one euro, but the project budget—not this estimate—is the hard boundary.

## Non-destructive preflight and budget check

```bash
task-03-deployment-strategy/scripts/gcp_ops.sh preflight \
  contract-assessment-dev europe-west3 dev 123456789012 ABCDEF-123456-ABCDEF
```

Prerequisites: authenticated `gcloud`, billing-budget viewer access, and the exact
billing account. The command verifies project-number binding, required APIs, an
active project-scoped budget, and lists Cloud Run/Cloud Tasks inventory. It makes no
changes. Success ends with `preflight passed`. It is safe to retry unchanged and
needs no cleanup.

## Post-deploy API smoke and Firestore deletion proof

```bash
export SMOKE_API_KEY_A='tenant-a-plaintext-key'
export SMOKE_API_KEY_B='tenant-b-plaintext-key'
task-03-deployment-strategy/scripts/api_smoke.sh \
  https://api.example.run.app review-001
unset SMOKE_API_KEY_A SMOKE_API_KEY_B
```

The smoke checks liveness, managed readiness, missing authentication, session
creation, cross-tenant concealment, run submission, typed SSE, cancellation, and
session deletion. The final authenticated `GET` must return `404` after `DELETE
/v1/sessions/{id}`; with the cloud configuration this exercises the Firestore
cascade rather than an ephemeral local database.

Success prints one summary line. The command creates at most one session and two
runs, then deletes the session. A failed run may leave that bounded session behind;
inspect sessions by the `smoke-<SMOKE_ID>` label and delete it through the same API.
Retry with a new opaque smoke ID after fixing the cause. Never place keys on the
command line or in a checked-in file.

## Runtime rollback to an existing revision

First identify a known healthy revision from deployment evidence, then run:

```bash
task-03-deployment-strategy/scripts/gcp_ops.sh rollback \
  contract-assessment-dev europe-west3 dev 123456789012 \
  sai-dev-api sai-dev-api-00002-abc
```

The command reads current traffic, proves the target revision is ready and belongs
to the named service, shifts 100% traffic with `gcloud run services update-traffic`,
and reads traffic again to verify the result. Expected incremental cost is zero
beyond normal request handling. Repeating the same command is idempotent.

Rollback changes Cloud Run traffic only. It never rebuilds an image, runs a historic
workflow, changes a Git ref, or creates a rollback commit. If the revision is absent
or unhealthy, stop and choose another existing healthy revision; rebuilding old
source is a separate, explicitly reviewed fallback.

## Reviewed environment teardown

Terraform currently protects the queue, Cloud Run services, secrets, and Firestore
from casual deletion. A full teardown therefore starts with a reviewed change that
disables only the intended deletion-protection controls and a reviewed destroy plan.
After that review, invoke the wrapper with the exact confirmation token:

```bash
task-03-deployment-strategy/scripts/gcp_ops.sh teardown \
  contract-assessment-dev europe-west3 dev 123456789012 sai \
  /absolute/path/to/task-03-deployment-strategy/terraform/environments/dev \
  DESTROY:contract-assessment-dev:dev
```

The script rechecks project-number binding, creates and prints a remote-state-backed
destroy plan, applies that exact plan, and verifies the API service, worker service,
and dispatch queue are absent. A wrong environment, project number, relative
Terraform root, or confirmation token fails before Terraform mutation. Do not retry
after a partial failure until the remote state lock and cloud inventory have been
reviewed.

Bootstrap WIF/state resources and any deliberately abandoned data resources are a
separate administrator-owned boundary. Inventory and delete them only under their
own reviewed plan; never delete the whole project merely to make this script pass.
