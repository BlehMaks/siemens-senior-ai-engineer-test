# C03 bootstrap Terraform

This stack is the manual bootstrap boundary for Task 3. It creates only the
state bucket, required APIs, workload identities, and optional GitHub workload
identity federation. Application resources stay out of scope until C04 and C05.

## What it guarantees

- no service-account keys;
- no primitive Owner or Editor roles;
- one service account per workload;
- GitHub OIDC trust narrowed to one repository and branch, with optional
  environment pinning;
- a versioned, non-public state bucket for later remote backend use.

## Honest limits

- The bootstrap stack intentionally has no `backend` block because it is the
  stack that creates the later remote backend bucket.
- Real `terraform init -backend=false` and `terraform validate` require a local
  Terraform CLI and provider plugins. This repository only ships deterministic
  file-level tests in CI-safe offline mode.
- Applying this stack still requires a human-held project-admin credential in a
  dedicated assessment project. That live plan is deferred to O13.
