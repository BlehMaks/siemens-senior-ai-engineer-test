# C04 destroy review

This stack is biased toward recoverability rather than aggressive teardown.

## Built-in safeguards

- Firestore delete protection is enabled by default.
- Firestore uses Terraform deletion policy `ABANDON`, so a casual `destroy`
  will not delete the assessment database.
- Secret containers have deletion protection enabled, so versions injected outside
  Terraform cannot disappear during a routine infrastructure teardown.
- The Artifact Registry repository uses immutable tags to keep release history
  inspectable.
- Both Cloud Run services have deletion protection enabled.
- The bootstrap-owned Cloud Tasks dispatch queue has `prevent_destroy` so queued
  work is not removed by a failed routine teardown.

## Operator expectation

If a full teardown is ever required, the reviewer should first disable Firestore
delete protection and switch `firestore_deletion_policy` from `ABANDON` to
`DELETE`, then disable Secret Manager deletion protection in a reviewed change.
Cloud Run deletion protection and the bootstrap queue's `prevent_destroy` must
also be disabled in their owning stacks before removing the execution plane.
These explicit changes keep destructive intent out of routine environment cleanup.
