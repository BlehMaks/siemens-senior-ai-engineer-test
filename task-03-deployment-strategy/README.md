# Task 3: Deployment strategy

## Assignment baseline

Describe how to deploy the Internet-search agent and API to a hyperscaler. The
strategy covers architecture, scalability, reliability, security, orchestration,
persistence, monitoring, and the services used. Diagrams make the assessment and
enterprise boundaries reviewable.

## Public deliverables

- [C01 cloud decision and target architecture](architecture/strategy.md) is the
  canonical provider comparison, low-cost assessment design, and Siemens-wide
  target.
- [ADR-0001](architecture/adr/0001-gcp-reference-profiles.md) records the conditional
  GCP decision and its reversal triggers.
- [C03 bootstrap Terraform](terraform/bootstrap/README.md) adds the executable
  identity and remote-state foundation for later GCP stacks.

GCP is selected for the executable assessment cell because Cloud Run, Cloud Tasks,
Firestore, Secret Manager, Artifact Registry, and Workload Identity Federation form
a small, testable path with negligible idle compute. This is not the claimed final
enterprise topology. The target uses jurisdictional cells, corporate identity,
governed ingress, isolated retrieval, regional state, a model gateway, and explicit
SLO/DR/FinOps/governance gates.

## Model and spend defaults

- Deterministic fake inference is the reproducible CI and cloud-smoke default.
- Local Ollama is the default real-model development and benchmark path.
- Cloud GPU infrastructure is disabled by default and remains budget gated.
- No live cloud apply, paid GPU, or public deployment is part of C01.

## Delivery status

| Item | Status after C01 | Required evidence |
|---|---|---|
| Provider and service decision | Designed | Weighted scorecard, reversal triggers, and official references |
| Assessment and enterprise topology | Designed | Container/plane and request-sequence diagrams with explicit boundaries |
| Container | Pending C02 | Locked multi-architecture build, non-root/read-only smoke, SBOM, scans |
| Identity and Terraform | C03 foundation shipped; C04-C07 pending | WIF/IAM, managed services, static policy, plan, rollback, teardown, budget checks |
| Enterprise migration | Pending C01A | Residency, SLO/RTO/RPO, IAM, DR, FinOps, and approval gates |
| Capacity proof | Pending C01B | Repeatable fake-provider load scenario and machine-readable measurements |

## Verification

C01 is documentation-only. Its checks cover Markdown structure, local links, diagram
blocks, official-source reachability, score arithmetic, and the submission audit.
Later implementation work must pass the repository gate:

```bash
make check
```

The C03 Terraform slice is intentionally bootstrap-only: it creates the state
bucket, required APIs, workload identities, and optional GitHub federation.
Managed services, Cloud Run, Cloud Tasks, monitoring, and budgets remain
deferred to C04-C07.

## Honest boundary

Apigee, Spanner, AlloyDB, GKE, Vertex AI, Cloud Run GPU, multi-cell routing,
corporate IdP integration, VPC Service Controls, and enterprise SIEM integration are
recommendations. C01 does not claim that they are provisioned, load tested,
security approved, or production ready.
