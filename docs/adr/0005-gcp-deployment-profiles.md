# ADR-0005: Use GCP for the assessment profile and a cell-based enterprise target

- Status: accepted
- Date: 2026-08-26

## Context

Task 3 needs executable infrastructure under a small budget and a strategy that can
evolve to a global industrial deployment. A single cheap Cloud Run stack is not a
complete enterprise topology.

## Decision

Implement the assessment profile with Cloud Run, Cloud Tasks, Firestore, Secret
Manager, Artifact Registry, and GitHub Workload Identity Federation. Minimum
instances and optional GPU/hardened-ingress modules default to zero or off.

Describe the enterprise target as independent regional or jurisdictional cells with
corporate identity, governed ingress, isolated public-web egress, Pub/Sub eventing,
a model gateway, residency-aware state, explicit SLO/DR/FinOps controls, and
centralized policy promotion. Place variable single-GPU inference on Cloud Run GPU
when it fits; use Vertex AI for approved managed inference, GKE Autopilot for
Kubernetes needs, and GKE Standard only for constraints Autopilot cannot satisfy.
Direct Compute Engine requires a separate ADR proving a VM-bound requirement.

## Alternatives

- Azure should replace GCP if Entra, Azure OpenAI/API Management, and the existing
  landing zone dominate the enterprise constraints.
- AWS should replace GCP if the existing foundation, Bedrock, and AWS event/data
  services dominate.
- An always-on GPU VM was rejected as the default because it conflicts with the
  assessment budget and is not a justified enterprise placement decision.

## Consequences

Terraform proves one low-cost reference cell, not a simulated global estate. The
same application contracts can move behind production adapters. Real cloud apply,
paid resources, and publication stay explicitly approval-gated.

