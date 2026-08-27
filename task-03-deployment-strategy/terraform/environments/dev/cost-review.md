# C04 cost review

The default `dev` posture stays intentionally close to the assignment's
low-cost assessment target.

## Default monthly cap

- Cloud Billing budget: EUR 10 when `billing_account_id`, `project_number`, and
  at least one explicit notification email are supplied.
- Thresholds: 50%, 90%, 100%.
- Default IAM recipients are disabled to avoid broad accidental mail fan-out.

## Low-idle design choices

- Cloud Firestore stays in one region and avoids an idle database VM.
- Secret Manager stores only empty containers until values are injected outside
  Terraform.
- Artifact Registry is one regional Docker repository.
- Logging prepares one dedicated bucket with 30-day retention; the C05 sink will
  route only the application logs needed by the service.

## Honest limit

The budget is an alert, not an enforcement boundary. Real spend control still
depends on later Cloud Run and Cloud Tasks caps in `C05`.
