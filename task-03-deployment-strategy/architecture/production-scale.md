# C01A — Enterprise production and migration gates

Status: reviewer-facing production addendum. It defines decision gates, not a claim
that Siemens production infrastructure has been deployed or approved.

This addendum turns the [reference architecture](strategy.md) into a staged
operating decision. It reuses the provider scorecard, jurisdictional-cell diagram,
request sequence, and runtime placement model there instead of duplicating them.
Measured workload inputs come from the
[capacity worksheet](capacity-model.md); assessment operations remain in the
[runbooks](runbooks.md).

## Migration path and exit criteria

Every phase preserves tenant authorization, durable accept-before-dispatch,
idempotent work, bounded retries, evidence-linked output, deletion, immutable
promotion, and a regional model-policy boundary. A phase advances only when its
exit evidence is recorded; company size alone is not a trigger.

| Phase | Smallest useful topology | Required exit evidence | Keep, scale, or replace |
|---|---|---|---|
| Assessment cell | One `dev` project; Cloud Run API/worker; Cloud Tasks; Firestore; fake model; direct baseline ingress | Local contract/capacity proof, reviewed Terraform plan, budget, smoke, rollback, teardown, and explicit live-test approval | Keep application contracts. Do not promote direct ingress, API keys, fake inference, or assessment IAM as enterprise controls. |
| Corporate pilot | One approved region and landing-zone project; corporate OIDC; governed HTTPS edge; regional model gateway; assessment data tier where permitted | Named tenant/data owners, privacy and threat review, measured p95 latency/queue age/cost, restore test, deletion evidence, incident ownership, and model approval | Scale Cloud Run/Tasks/Firestore only while quotas, consistency, SLO, and unit cost pass. |
| Department rollout | Separate nonprod/prod projects; business-unit quotas; internal connector isolated from public-web egress; canary promotion | Department concurrency and destination limits, connector ACL tests, DLP/residency checks, error-budget policy, on-call rehearsal, and chargeback attribution | Add Pub/Sub when fan-out or throughput requires it; move sustained workers/models only on measured placement triggers. |
| Regional cell | Independent control, workload, data, and model planes for one jurisdiction; metadata-only global router | Cell load and failure test, backup/restore plus evacuation drill, regional key/log policy, queue replay test, RTO/RPO evidence, and security approval | Evaluate Spanner or AlloyDB/PostgreSQL; retain Firestore only if its transaction/query/operating envelope still passes. |
| Global governed rollout | Multiple jurisdictional cells; central policy/artifact promotion and permitted aggregate telemetry | Cell routing and fail-closed residency tests, cell-level canary/rollback, cross-region incident game day, audit evidence, vendor capacity, and business continuity sign-off | Add active-active only for qualified cells. Never centralize prompts, evidence, memory, or restricted content merely for routing convenience. |

Rollback is phase-local: shift traffic to an existing healthy artifact or policy
version, or route to a qualified cell. It never rebuilds historical source or
rewrites Git history.

## Placement, data, edge, and recovery triggers

| Decision | Keep the simpler option while | Promote or replace only when | Required proof |
|---|---|---|---|
| Cloud Run service | Request-triggered API/orchestration fits timeout, concurrency, startup, network, and scale-to-zero behavior | Sidecars, network policy, warm capacity, sustained consumers, or accelerator topology are measured requirements | Representative load, startup distribution, saturation and cost per successful run |
| Cloud Run job | Work is finite, schedulable, and has a bounded completion window | Continuous consumption or queue-aware scaling is required | Retry/cancel/restart test and maximum execution budget |
| Cloud Run worker pool | A continuous pull worker fits supported regional/runtime constraints | Service mesh, stronger isolation, custom autoscaling, or Kubernetes scheduling is required | Backlog recovery, disruption, rollout, and idle-cost comparison |
| Cloud Run GPU | Variable single-GPU demand passes quota, cold-start, model-load, license, residency, and cost gates | Utilization is sustained, multi-GPU, shared, or topology-aware | Quality/latency benchmark, quota grant or increase, regional capacity check, maximum-instance cap, fallback exercise, security review, teardown |
| GKE Autopilot | Kubernetes is justified but node-level control is not | Unsupported privilege, driver, daemon, accelerator, or topology control is proven | Policy, upgrade, disruption, autoscaling, and total-operations-cost test |
| GKE Standard | Autopilot cannot meet a documented node/runtime constraint | Never by preference alone | Separate ADR naming the exact unsupported requirement and operational owner |
| Vertex AI | An approved managed model meets task quality, region, data terms, latency, observability, and economics | Route to self-hosted/on-prem only when one of those gates fails and the alternative is proven | Versioned evaluation, contract/privacy approval, quota and fallback exercise |
| Firestore | Regional control state fits transactions, indexes, hot-key limits, restore posture, and unit cost | Strongly consistent multi-region control state or relational constraints exceed the measured envelope | Production-shaped load, restore, schema evolution, deletion and cost evidence |
| Spanner | Strong consistency, horizontal scale, availability, and cell topology justify its operational and financial floor | Reconsider if a regional relational workload is sufficient | Access-pattern model, split/hotspot test, migration rehearsal, RTO/RPO and cost |
| AlloyDB/PostgreSQL | Regional SQL, joins, constraints, ecosystem, and team skills dominate | Do not use it as a substitute for an unexamined global consistency requirement | Connection/autoscaling model, HA/restore test, migration and maintenance ownership |
| Direct Cloud Run edge | Only an assessment endpoint is exposed and its public-invoker risk is explicitly accepted | Corporate pilot begins | Auth abuse test, quotas, no privileged route, accepted residual risk |
| HTTPS LB + Cloud Armor | A governed edge, WAF/DDoS controls, and no default-URL bypass meet product needs | Apigee is justified by API-product lifecycle, onboarding, policy, or analytics | Origin-bypass test, policy tests, certificate/DNS ownership, cost |
| Apigee | Cross-team API products and governance justify the platform | Re-score Azure/AWS if the enterprise landing zone or commercial model dominates | Platform-owner approval, policy portability, latency, availability, and TCO |
| Single-region recovery | Restore and regional evacuation satisfy the agreed impact class | Concurrent regional service is required by measured business impact | Repeated restore/evacuation drill with reconciled run, event, idempotency, and memory state |
| Active-active cells | Residency-safe routing and conflict ownership are proven | Never before independent cells pass restore and isolation gates | Traffic-loss test, stale-route test, cell canary/rollback, conflict and cost model |

The GCP choice remains conditional under [ADR-0001](adr/0001-gcp-reference-profiles.md):
approved Azure or AWS landing-zone, identity, model, region, or commercial constraints
trigger a provider re-score before production commitment.

## SLO and disaster-recovery contract

These are planning hypotheses for discovery, not external commitments. Business
impact analysis may tighten or relax them, but availability, latency, RTO, RPO,
data topology, staffing, and cost must change together.

| Service class | Availability hypothesis | Latency and outcome indicators | Recovery hypothesis | Evidence owner |
|---|---|---|---|---|
| Assessment | Best effort; no production SLO | Local thresholds only; no production-capacity claim | Disposable runtime; retained state follows the documented teardown boundary | Assignment operator |
| Corporate pilot | 99.95% monthly submission/status | p95 regional submission under 300 ms excluding IdP; first event, successful completion, citation validity, and policy correctness measured separately | Candidate RTO 4 h / RPO 24 h, accepted only after a restore drill | Product owner + SRE + data owner |
| Regional production cell | Candidate 99.99% monthly submission/status only after a multi-region runtime/data topology and health-based failover prove it; a single-region Cloud Run cell cannot infer this target from the provider SLA | Per-query-class completion and quality SLOs; queue age and model/destination saturation are leading signals | Candidate RTO 60 min / RPO 5 min, proven for run, event, idempotency, quota, and governed memory state | Business continuity + SRE + platform data owner |
| Global service | Derived from qualified cells and router, never multiplied from provider SLAs | Correct residency routing and cell availability are separate objectives | RTO/RPO remain cell/data-class specific; restricted content is not copied globally to improve a headline SLA | Global product + legal/privacy + cell owners |

Consistency rules:

- accepted work is reconciled from durable state; queue retention is not an RPO;
- an availability target without staffed response, error-budget action, synthetic
  probes, and rollback ownership is rejected;
- a database SLA without tested backup, restore, schema migration, and regional
  evacuation evidence is rejected;
- completion SLOs are segmented by public-site, internal-connector, model, and query
  class so a dependency outage cannot be hidden in one average;
- active-active is rejected when residency, idempotency, conflict resolution, model
  availability, or unit cost has no passing test.

## Tenant and data-residency matrix

The routing key is `tenant + jurisdiction + data_classification + tool_class`.
Unknown or conflicting attributes fail closed before retrieval or inference.

| Data/tool class | Cell placement | Global plane may retain | Required controls | Forbidden shortcut |
|---|---|---|---|---|
| Public-web query and evidence | User's permitted jurisdictional cell | Opaque run/cell ID and approved aggregate service metrics | URL/DNS/redirect policy, download limits, retention/deletion, evidence provenance | Giving the public worker a route to corporate or OT networks |
| ACL-protected enterprise knowledge | Cell approved by source owner and tenant policy | Connector health and redacted aggregate counts | Corporate identity delegation, source ACL preservation, purpose/audit tags, separate connector identity | Reusing the public-web fetcher or copying content to a global index |
| Personal/confidential content and memory | In-country/in-region cell selected by policy | Policy/version metadata only | Encryption/key policy, minimization, evidence/confidence/expiry, subject deletion, legal hold | Cross-cell replication without a recorded legal basis and data-owner approval |
| Restricted/export-controlled data | Dedicated approved cell or no processing | No content-derived telemetry | Explicit allowlist, isolated model/egress, customer-managed controls where required, immutable audit | Fallback to a model, region, or vendor outside the approved boundary |
| OT/plant systems | Separate connector plane after OT security approval | Availability metadata permitted by policy | Brokered read-only interface, protocol allowlist, segmentation, plant owner, emergency disable | Direct Internet-research worker access to an OT network |

Deletion propagates through run state, events, evidence, memory, indexes, caches,
artifacts, and permitted analytics. Legal hold is an explicit policy state, not a
silent exception. The global router stores no prompt or evidence body.

## Identity and access review

Production separates bootstrap authority from routine deployment and runtime. The
assessment Terraform demonstrates a small cell; its broad resource-creation roles
are not a reusable enterprise entitlement.

| Principal | Allowed purpose | Review proof |
|---|---|---|
| Workforce user | Submit/read/cancel/delete only authorized tenant resources | Corporate token audience, tenant/action policy, revocation and negative cross-tenant tests |
| API runtime | Tenant-scoped state and queue enqueue | No worker invocation, secret, model, or unrelated project access |
| Queue caller | Invoke only the owning worker with the expected OIDC audience | Authoritative worker invoker binding; no enqueue, state, or deploy permission |
| Worker runtime | Claim/update authorized runs, append events, use approved retrieval/model routes | No public invocation, tenant predicates, egress/model policy and secret scope |
| Model gateway | Route approved structured requests to approved model versions | No tenant authorization decisions or arbitrary tool access |
| CI identity | Exchange repository/environment-bound OIDC for a short-lived deploy session | Immutable repository ID/ref/environment conditions; no static key |
| Deployer | Apply the reviewed artifact and plan to named environment resources | No project IAM admin, service-account-policy admin, application entity read, or arbitrary `actAs`; plan/artifact binding |
| Bootstrap administrator | Create landing-zone APIs, state, federation, and initial identities | Separate invocation, approval, audit, and break-glass controls; absent from routine deploy |
| Migration job | Execute one versioned schema/data transition | Time-bound identity, forward/backward compatibility, backup and reconciliation |
| Observability exporter | Write redacted telemetry to approved sinks | Cannot read prompts/evidence/secrets or change runtime policy |

For each phase, export the effective IAM graph and reject primitive roles, wildcard
principals, service-account keys, cross-project principals, collapsed identities,
unbounded `actAs`, and any policy editor capable of self-escalation. Every exception
has an owner, expiry, reason, and removal test.

## FinOps unit-cost worksheet

Use measured values, not public-list-price arithmetic alone. The authoritative
formulas are in the [capacity model](capacity-model.md).

| Dimension | Required measurement | Promotion/stop rule |
|---|---|---|
| Demand | Active tenants/users, peak runs/s, burst, in-flight duration, pages and tokens per run | Reject a capacity plan that omits peak concentration and destination/model quotas |
| Quality denominator | Successful and independently quality-accepted runs | Track both cost per successful run and quality-adjusted cost; cheap failed answers do not count |
| Variable cost | Model input/output, retrieval/search, egress, queue, storage operations, logs/traces | Attribute by tenant, query class, tool, model, cell, and artifact version |
| Fixed cost | Edge/API platform, warm capacity, GKE control/operations, committed model capacity, security/log sinks | Add a managed platform only when its governance or load value exceeds its floor cost |
| Waste | Retries, duplicate work, idle GPU, abandoned artifacts, excess telemetry, failed fetch/model calls | Set a named alert and remediation owner for each material waste class |
| Guardrails | Tenant admission, queue dispatch/concurrency/age, worker/model maxima, destination limits, budgets | Budgets alert; runtime caps stop spend. Exhaustion degrades to an approved smaller model, evidence-only answer, deferral, or explicit failure |

Each phase records region/currency/date, negotiated discounts, model version, quality
threshold, observation window, confidence range, and owner. A promotion is blocked
when unit cost, quality, quota headroom, or teardown cost is unknown.

## Approval ledger and validation checklist

| Decision/evidence | Accountable owner | Gate |
|---|---|---|
| Business criticality, user journeys, availability and recovery | Product owner + business continuity | Signed impact class and tested acceptance criteria |
| Tenant, classification, residency, retention, deletion and legal hold | Data owner + privacy/legal | Completed matrix and deletion/restore evidence |
| Corporate identity, service identities, break glass and IAM graph | IAM/platform security | Least-privilege audit and escalation-path review |
| Public/internal/OT retrieval boundaries and egress | Network + product/OT security | Threat model, segmentation and negative route tests |
| Model quality, license, data terms, safety and fallback | Model risk + legal/procurement | Versioned evaluation and fallback exercise |
| Capacity, SLO, error budget, incident and DR | SRE + cell operator | Load/failure/restore/evacuation evidence |
| Unit cost, quotas, commitments and chargeback | FinOps + product owner | Quality-adjusted cost worksheet and budget controls |
| Provider and regional service selection | Enterprise architecture + procurement | Re-scored ADR with verified landing-zone and commercial inputs |

Open decisions remain blockers, not TODO-shaped assumptions: actual identity estate,
allowed regions, data classes, tenant hierarchy, source systems, OT boundary, traffic
shape, model/vendor terms, retention/legal hold, recovery impact classes, platform
ownership, on-call coverage, and negotiated cloud economics. The architecture review
question remains: **would this work for thousands of employees across regional legal
boundaries, and which evidence proves each boundary rather than merely naming it?**

## Explicit non-claims

The repository does not claim that Apigee, Spanner, AlloyDB, Pub/Sub, GKE, Vertex
AI, Cloud Run GPU, active-active routing, corporate IdP integration, VPC Service
Controls, CMEK, enterprise SIEM, internal connectors, or OT integration were
deployed or approved. They are gated production options. The tested public artifact
is the bounded assessment cell and its shared application contracts.
