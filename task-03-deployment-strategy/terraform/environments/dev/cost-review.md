# Development cost review

The default `dev` posture stays intentionally close to the assignment's
low-cost assessment target.

## Default alert budget

- Cloud Billing budget: EUR 5. The wrapper supplies the linked billing account,
  project number, and a monitored email recipient.
- Thresholds: 20%, 50%, 80%, 100%.
- Default IAM recipients are disabled to avoid broad accidental mail fan-out.

## Low-idle design choices

- Cloud Firestore stays in one region and avoids an idle database VM.
- Secret Manager holds two small random secret versions seeded by the bootstrap;
  the payloads never enter Terraform state.
- Artifact Registry is one regional Docker repository.
- Logging prepares one dedicated bucket with 30-day retention; the C05 sink will
  route only the application logs needed by the service.
- API and worker minimum instances are zero, and each service has a one-instance
  maximum in `dev`.
- The queue defaults to one dispatch per second, one concurrent delivery, five
  attempts, and a 15-minute retry window.
- The paid load-balancer and Cloud Armor posture is documented but remains off
  in the default `baseline` mode.

## Honest limit

The budget is an alert, not an enforcement boundary. Scale-to-zero, one-instance
Cloud Run maxima, and the one-delivery queue limit are the runtime controls. Stop
the smoke and investigate as soon as an unexpected alert arrives.
