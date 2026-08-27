# Development cost review

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
- API and worker minimum instances are zero, with default maximums of three and
  five respectively.
- The queue defaults to one dispatch per second, one concurrent delivery, five
  attempts, and a 15-minute retry window.
- The paid load-balancer and Cloud Armor posture is documented but remains off
  in the default `baseline` mode.

## Honest limit

The budget is an alert, not an enforcement boundary. The Cloud Run and Cloud
Tasks caps limit work amplification, but an operator must still monitor spend
and disable workloads if the budget threshold is crossed.
