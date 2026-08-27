# C03 bootstrap Terraform

This stack is the manual bootstrap boundary for Task 3. It creates the state
bucket, required APIs, workload identities, empty protected secret containers,
their runtime access policies, and optional GitHub workload identity federation.
Routine application resources stay out of scope until C04 and C05.

## What it guarantees

- no service-account keys;
- no primitive Owner, Editor, or Viewer roles;
- one service account per workload;
- reviewed predefined roles plus a small database/budget/Cloud Tasks/Cloud Run
  lifecycle
  custom role for the application deployer, with no project-IAM, Firestore entity,
  direct Cloud Run invocation, or Cloud Run SSH permissions;
- `serviceAccountUser` only on the three runtime identities it must attach and
  `storage.objectAdmin` only on the Terraform state bucket;
- project-level `datastore.user` bindings are bootstrap-owned and limited to the
  API and worker identities, so the application deployer cannot grant itself data
  access;
- regional, deletion-protected secret containers and their resource-scoped runtime
  access are bootstrap-owned; payload versions remain out of Terraform and the
  application deployer has no Secret Manager administration or access role;
- the Cloud Tasks service-agent token grant, worker invoker, public API invoker,
  and queue data-plane bindings are bootstrap-owned; the deployer cannot mutate
  service, queue, or service-account IAM policies;
- GitHub OIDC trust anchored to one immutable numeric repository ID and also
  narrowed to the expected owner/repository name and branch, with optional
  environment pinning;
- a versioned, non-public state bucket for later remote backend use.

## Honest limits

- The bootstrap stack intentionally has no `backend` block because it is the
  stack that creates the later remote backend bucket.
- The committed provider lock is generated with Terraform 1.9 for Darwin ARM64
  and Linux AMD64. Validation still needs the locked providers to be available
  in the local Terraform cache.
- Applying this stack still requires a human-held project-admin credential in a
  dedicated assessment project. That live plan is deferred to O13.
- The deployer can update Cloud Run services and attach the three named runtime
  identities. That is an intentional high-trust release capability: deployed code
  runs with the selected identity. Repository-ID/ref/environment-bound WIF,
  protected approvals, immutable image digests, and exact plan/apply binding are
  therefore security boundaries, not optional process controls.
- Runtime resources do not exist on the first bootstrap apply. Leave
  `enable_runtime_policy = false`, create the reviewed application resources,
  then reapply this stack with `enable_runtime_policy = true`. Set
  `api_allow_unauthenticated` to match the application ingress mode. Later
  application deploys never need IAM-policy mutation permissions.
- The deployer custom role uses the single-project budget permissions documented
  by Google Cloud. If the selected billing-account policy does not permit that
  flow, an administrator must grant the external billing permission before O13.
