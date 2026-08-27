# C03 bootstrap Terraform

This stack is the manual bootstrap boundary for Task 3. It creates only the
state bucket, required APIs, workload identities, and optional GitHub workload
identity federation. Application resources stay out of scope until C04 and C05.

## What it guarantees

- no service-account keys;
- no primitive Owner, Editor, or Viewer roles;
- one service account per workload;
- a reviewed deployer role allowlist for the application stack and
  `serviceAccountUser` only on the three runtime identities it must attach;
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
- An enabled budget additionally needs `roles/billing.costsManager` on the
  selected billing account. Project-scoped bootstrap Terraform cannot grant that
  external permission.
