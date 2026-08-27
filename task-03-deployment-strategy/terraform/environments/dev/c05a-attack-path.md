# C05A attack-path review

## Baseline mode

- API entry stays on the direct Cloud Run URL because this is the cheapest
  assessment path.
- Public reachability does not make worker invocation public: only the worker
  service gets `roles/run.invoker`, and only for the dedicated Cloud Tasks
  caller identity.
- The API runtime can enqueue only on the single reviewed queue.

## Hardened mode

- API ingress accepts only internal and Cloud Load Balancing traffic, and the
  default `run.app` URL is disabled, so direct internet requests cannot bypass
  the reviewed edge.
- A later authenticated HTTPS load balancer plus Cloud Armor becomes mandatory.
- The API does not keep an unauthenticated Cloud Run IAM binding in this mode;
  external access must be added explicitly with the reviewed front door.
- The worker still stays behind internal load-balancer ingress and does not
  rely on the public API edge.

## Worker delivery

- The worker keeps its default `run.app` URL because Cloud Tasks requires that
  endpoint, while the worker ingress policy recognizes same-project Cloud Tasks
  traffic as internal.
- Cloud Run IAM still requires an OIDC token minted for the dedicated Tasks
  caller identity. Neither the public API principal nor `allUsers` receives
  worker invocation rights.
- The API references only its pepper secret; the worker references only the
  task-signing HMAC, matching the resource-specific Secret Manager grants.

## Honest boundary

This Terraform slice defines the infrastructure guardrails, not the final Task 2
worker handler. Until `C05B/C05C` land, the reserved queue path is a contract
only, not a fully wired application endpoint.

The ingress and default-URL decisions follow the official
[Cloud Run ingress contract](https://cloud.google.com/run/docs/securing/ingress),
which lists same-project Cloud Tasks as internal traffic and warns that disabling
the default URL prevents Cloud Tasks delivery.
