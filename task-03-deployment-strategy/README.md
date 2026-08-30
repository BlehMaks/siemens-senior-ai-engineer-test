# Task 3: Deployment strategy

## Assignment baseline

This task describes and implements a deployment path for the Internet-search
agent and its API. The executable assessment cell runs on Google Cloud. The wider
enterprise design is documented separately so that future recommendations are not
mistaken for deployed resources.

Only Tasks 1 to 3 belong to this cloud runtime. Tasks 4 to 6 stay local.

## Executable assessment cell

The assessment profile uses:

- Cloud Run for the API and worker;
- Cloud Tasks for authenticated asynchronous dispatch;
- Firestore for durable application state;
- Secret Manager for runtime secrets;
- Artifact Registry for digest-pinned images;
- Cloud Logging, monitoring, and an operator-selected test alert budget;
- GitHub Actions with Workload Identity Federation for keyless delivery.

The LLM engine is not deployed. Deterministic fake inference is the CI and cloud
smoke default. Local Ollama remains the real-model development path.

The submission also includes a separate, disabled-by-default production model
plane. It can provision a private Cloud Run L4 service from an approved,
digest-pinned Ollama-compatible image. It is not referenced by the assessment
workflow and requires an explicit GPU cost acknowledgement before apply. See
[the production model-plane root](terraform/environments/production-model-plane/README.md).

## Provisioning entry point

All GCP and GitHub configuration changes begin with
[`scripts/bootstrap.sh`](scripts/bootstrap.sh). The wrapper calls Terraform for
provisioning and can plan, apply, verify, or dispatch deployment:

```bash
# One-time operator login. The wrapper never calls gcloud.
gcloud auth application-default login
gcloud auth application-default set-quota-project PROJECT_ID

./task-03-deployment-strategy/scripts/bootstrap.sh plan \
  PROJECT_ID OWNER/REPOSITORY REVIEWER europe-west3

./task-03-deployment-strategy/scripts/bootstrap.sh deploy \
  PROJECT_ID OWNER/REPOSITORY REVIEWER europe-west3
```

When the correlated Actions run shows `Waiting`, open that run and choose
**Review deployments**, select `gcp-dev`, and choose **Approve and deploy**.
Repeat the review if the protected apply job asks again. The wrapper remains the
operator process and Terraform remains the only provisioning engine.

The first Terraform root creates separate private buckets for privileged
bootstrap state and application delivery state. The main bootstrap then uses its
GCS backend to configure APIs, identities, IAM, direct GitHub-to-deployer WIF,
the protected GitHub Environment and variables, secret containers and initial
versions, and the authenticated queue. The protected GitHub workflow builds and
applies the application stack. Its first job binds the dispatch to the exact
verified `master` SHA and fails before cloud authentication if the branch moved.

The one-time ADC login succeeds when the plan reads
`projects/siemens-senior-ai-engineer`, reports project number `163220015018`,
and shows the two state buckets on an empty project. A project permission error
usually means ADC was created for another Google account.

GitHub must also permit branch protection and a protected Environment with a
required reviewer. GitHub Free, Pro, and Team provide the reviewer rule only for
public repositories; a private repository needs GitHub Enterprise. Terraform
stops instead of weakening these controls when the plan does not support them.

See [release and operations](../docs/release-and-operations.md) for the complete
clean-machine and cloud procedure. The [resource and IAM manifest](../docs/cloud-resource-manifest.md)
lists the exact changes made by the default deployment.

## Architecture deliverables

- [Cloud decision and target architecture](architecture/strategy.md)
- [Capacity model and local proof](architecture/capacity-model.md)
- [Production-scale gates](architecture/production-scale.md)
- [GCP profile decision record](architecture/adr/0001-gcp-reference-profiles.md)
- [Bootstrap Terraform](terraform/bootstrap/README.md)
- [Gated production model plane](terraform/environments/production-model-plane/README.md)
- [CI/CD contract](operations/ci-cd.md)
- [Assessment-cell runbooks](architecture/runbooks.md)

## Current evidence

| Area | Evidence |
|---|---|
| Container | Locked multi-stage image, non-root runtime, local smoke, vulnerability scan and SBOM workflow |
| Terraform | Format, validation, static tests, provider-mocked resource contracts, and a read-only plan against the target project |
| Identity | Repository-scoped WIF, short-lived credentials, separate runtime identities, and narrow resource ownership |
| Delivery | Protected GitHub Environment, exact image digest, binary plan handoff, and non-cancelling apply concurrency |
| Operations | Local API smoke, cloud preflight, existing-revision rollback, and guarded teardown procedures |
| Capacity | Repeatable fake-provider proof with machine-readable sample output |

## Honest boundary

The assessment cell is a low-cost executable reference, not the final Siemens
topology. Apigee, GKE, Spanner or AlloyDB, Vertex AI, Cloud Run GPU, multi-cell
routing, corporate identity, VPC Service Controls, and enterprise SIEM remain
gated options. Production claims still require residency, SLO, recovery, cost,
model, security, and traffic decisions from the owning teams.
