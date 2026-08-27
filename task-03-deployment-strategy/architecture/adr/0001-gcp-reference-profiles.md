# ADR-0001: Use GCP for the assessment cell and a conditional enterprise target

- Status: accepted for C01
- Date: 2026-08-27
- Detailed design: [C01 cloud decision and target architecture](../strategy.md)

## Context

The assignment needs an executable cloud path with negligible idle cost and an
honest production strategy for a global industrial enterprise. A single serverless
project can prove packaging, identity, async execution, persistence, rollback, and
cost controls, but cannot prove corporate identity, residency, multi-cell recovery,
or enterprise model governance.

## Decision

Use GCP for the reference implementation and treat the choice as conditional on
enterprise discovery.

- The assessment cell uses Cloud Run, Cloud Tasks, Firestore, Secret Manager,
  Artifact Registry, and Workload Identity Federation. CPU services scale to zero.
- Deterministic fake inference is the CI/cloud-assessment default; local Ollama is
  the default real-model development path. No GPU is provisioned by default.
- A cloud GPU path remains disabled until an explicit budget, quota, model-license,
  load, latency, security, and data-residency gate passes.
- The enterprise target uses independent jurisdictional cells, corporate identity,
  governed ingress, isolated retrieval planes, Pub/Sub eventing, regional state,
  and a model gateway. Cloud Run, GKE, Vertex AI, and on-premises inference are
  placement choices behind stable application contracts.
- Direct Compute Engine is not a baseline. It requires a separate ADR showing a
  VM-bound OS, kernel, driver, appliance, or hardware constraint that Cloud Run,
  GKE, and Vertex AI cannot meet.

## Reversal triggers

- Prefer Azure if an approved Azure landing zone, Entra, API Management, Azure AI,
  regional availability, and commercial commitments dominate the requirements.
- Prefer AWS if an approved AWS foundation, IAM Identity Center, API Gateway,
  EventBridge/SQS, DynamoDB/Aurora, Bedrock, regional availability, and commercial
  commitments dominate.
- Re-score all providers if legal, residency, corporate identity, existing platform
  operations, model terms, or total cost invalidates the current assumptions.

## Consequences

One low-cost GCP cell will be implemented and tested; the enterprise topology stays
a design until later delivery gates provide capacity, security, recovery, and cost
evidence. Provider-specific code must remain behind identity, queue, repository,
observability, and model-gateway ports.
