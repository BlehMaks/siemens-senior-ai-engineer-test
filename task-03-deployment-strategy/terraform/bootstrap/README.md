# C03 bootstrap Terraform

This stack is the manual bootstrap boundary for Task 3. It creates only the
state bucket, required APIs, workload identities, and optional GitHub workload
identity federation. Application resources stay out of scope until C04 and C05.

## What it guarantees

- no service-account keys;
- no primitive Owner, Editor, or Viewer roles;
- one service account per workload;
- reviewed predefined roles plus a small database/budget custom role for the
  application deployer, with no project-IAM or Firestore entity permissions;
- `serviceAccountUser` only on the three runtime identities it must attach,
  a three-permission custom policy role only on the Cloud Tasks caller identity,
  and `storage.objectAdmin` only on the Terraform state bucket;
- project-level `datastore.user` bindings are bootstrap-owned and limited to the
  API and worker identities, so the application deployer cannot grant itself data
  access;
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
- The deployer custom role uses the single-project budget permissions documented
  by Google Cloud. If the selected billing-account policy does not permit that
  flow, an administrator must grant the external billing permission before O13.
