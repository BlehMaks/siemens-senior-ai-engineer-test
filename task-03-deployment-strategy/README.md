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
- [C01B capacity model and local proof](architecture/capacity-model.md) provides the
  executable fake-provider load scenario and machine-readable sample result.
- [C01A production-scale gates](architecture/production-scale.md) define the staged
  pilot-to-global migration, placement/data/edge/recovery triggers, SLO/DR contract,
  residency and IAM reviews, and FinOps worksheet.
- [ADR-0001](architecture/adr/0001-gcp-reference-profiles.md) records the conditional
  GCP decision and its reversal triggers.
- [C03 bootstrap Terraform](terraform/bootstrap/README.md) adds the executable
  identity and remote-state foundation for the assessment stack.

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
- No live cloud apply, paid GPU, or public deployment has been performed or claimed.

## Delivery status

| Item | Current public status | Evidence boundary |
|---|---|---|
| Provider and service decision | Designed | Weighted scorecard, reversal triggers, and official references |
| Assessment and enterprise topology | Designed | Container/plane and request-sequence diagrams with explicit boundaries |
| Container | C02 shipped and locally verified | Locked multi-architecture build, non-root/read-only smoke, SBOM and local scans; no deployed image claim |
| Identity and Terraform | C03-C05C shipped and credential-free validated | Bootstrap, managed services, bounded execution, cloud adapters and integration; real provider plan remains C08 |
| Keyless delivery | C06 shipped and statically/lint verified | Pinned PR gates, WIF plan/apply, immutable digest and protected-environment contract; external GitHub settings remain operator prerequisites |
| Operations | C07 shipped and locally verified | Preflight, bounded API smoke, existing-revision rollback and exact-plan teardown; no live project execution claim |
| Enterprise migration | C01A shipped as gated guidance | Residency, SLO/RTO/RPO, IAM, DR, FinOps, validation owners and explicit non-claims |
| Capacity proof | C01B local proof shipped | Repeatable fake-provider load scenario and machine-readable measurements |
| Real assessment cell | Not executed | C08 requires a supplied project, billing budget, protected approval, evidence capture and verified cleanup |

## Verification

The normal repository gate covers formatting, lint, typing, tests, and the public
submission audit:

```bash
make check
```

Terraform modules and environments additionally pass credential-free `fmt`,
`validate`, and mock-plan contracts. Those checks prove configuration structure and
negative policy cases; they do not replace the approval-gated real plan in C08.

Operational preflight, smoke, existing-revision rollback, and guarded teardown are
documented in [the assessment-cell runbooks](architecture/runbooks.md).

## Honest boundary

Apigee, Spanner, AlloyDB, GKE, Vertex AI, Cloud Run GPU, multi-cell routing,
corporate IdP integration, VPC Service Controls, and enterprise SIEM integration are
recommendations. C01 does not claim that they are provisioned, load tested,
security approved, or production ready.
