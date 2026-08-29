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
well below one euro. The EUR 5 project budget alerts the operator; the runtime and
queue caps are the controls that bound demand because a budget does not stop spend.

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

Ordinary HTTP probes have a 15-second wall-clock limit. The SSE probe has a
30-second limit and requires HTTP `200` plus a typed `run.*` event; an event-shaped
error response cannot pass the smoke check.

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

## Reviewed application teardown

Terraform protects the bootstrap-owned queue, Cloud Run services, secrets, and
Firestore from casual deletion. Application teardown starts with a reviewed change
that disables only the Cloud Run and application-data deletion controls followed by
a reviewed application-stack destroy plan. Invoke the wrapper with the exact
confirmation token:

```bash
task-03-deployment-strategy/scripts/gcp_ops.sh teardown \
  contract-assessment-dev europe-west3 dev 123456789012 sai \
  /absolute/path/to/task-03-deployment-strategy/terraform/environments/dev \
  DESTROY:contract-assessment-dev:dev
```

The script rechecks project-number binding, creates a remote-state-backed destroy
plan, verifies the plan's project ID, project number, region, and system code against
the confirmation target, prints it, and applies that exact plan. It then verifies
the API and worker services are absent. Inventory permission or transport failures
stop verification instead of being treated as absence. A wrong environment,
project number, plan binding, relative Terraform root, or confirmation token cannot
reach `terraform apply`. Do not retry after a partial failure until the remote state
lock and cloud inventory have been reviewed.

The dispatch queue is intentionally not queried or deleted by this application
command. It belongs to the bootstrap state together with WIF, identities, state
storage, secret containers, runtime IAM, and the service-agent token grant. After
application teardown is verified, an administrator may separately review a
bootstrap destroy plan with queue `prevent_destroy` and the intended secret/state
protections explicitly disabled. Never delete the whole project merely to make
either teardown pass.
