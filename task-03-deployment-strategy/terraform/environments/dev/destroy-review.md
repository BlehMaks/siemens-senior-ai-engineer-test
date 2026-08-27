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

## Operator expectation

If a full teardown is ever required, the reviewer should first disable Firestore
delete protection and switch `firestore_deletion_policy` from `ABANDON` to
`DELETE`, then disable Secret Manager deletion protection in a reviewed change.
That makes destructive intent explicit instead of burying it inside a routine
environment cleanup.
