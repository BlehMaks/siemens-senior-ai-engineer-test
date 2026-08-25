# Task 3: Deployment strategy

## Assignment baseline

Describe how to deploy the Internet-search agent and API to a hyperscaler. The strategy must cover the architecture, scalability, reliability, security, and the services or tools used for orchestration, persistence, and monitoring. The required deliverable is a detailed deployment plan with architecture diagrams where useful.

## Recommended platform and stack

Google Cloud is the working recommendation because its managed serverless, queueing, identity, model-hosting, and observability services map cleanly to the system. The final decision should be checked against model availability, region, cost, and the live-deployment budget.

- Terraform for Infrastructure as Code, with reviewable plans and remote state.
- Cloud Run for the stateless API and orchestration worker.
- Cloud Tasks for bounded asynchronous run dispatch and retry control; Pub/Sub only where fan-out events are required.
- A managed open-model endpoint through Vertex AI when it can run the selected model family economically. The local Ollama runtime remains a development adapter.
- Cloud SQL for PostgreSQL and, if justified by Task 1 evaluation, `pgvector` for semantic memory.
- Cloud Storage for large research artifacts with lifecycle and retention policies.
- Secret Manager for application secrets and external credentials.
- Artifact Registry for immutable container images.
- External Application Load Balancer or API gateway controls, plus Cloud Armor where the exposed path and threat model justify it.
- Cloud Logging, Monitoring, Trace, Error Reporting, and alerting tied to service-level objectives.
- GitHub Actions with Workload Identity Federation. CI receives no long-lived Google Cloud service-account key.

This design uses managed services and scale-to-demand paths instead of leaving a GPU virtual machine running. A dedicated GPU deployment is acceptable only if measured traffic, latency, and data constraints make the managed endpoint unsuitable.

## Engineering extension

The assignment asks for a written strategy. The proposed extension is an executable Terraform implementation, container build, CI/CD workflow, and smoke-tested deployment in a dedicated project supplied later.

Tasks 1 to 3 share one logical system but keep independent deployable boundaries. The model provider is an adapter, which allows local Ollama tests and cloud inference without forking agent behavior.

## Zero-trust and IAM requirements

- Give every runtime and CI component its own service account.
- Grant the minimum roles to the specific resource, not broad project-level editor roles.
- Use authenticated service-to-service calls and deny unauthenticated access unless the public API entry point explicitly requires it.
- Keep databases and internal services on private paths where supported; control egress for the web-research worker.
- Use Workload Identity Federation for GitHub Actions and short-lived credentials for operators.
- Store secrets only in Secret Manager and mount or fetch them for the workload identity that needs them.
- Separate deploy, runtime, migration, and observability permissions.
- Enable audit logs and alert on policy, secret, and service-account changes.

## Reliability, scale, and cost constraints

- Define independent scaling and concurrency for API, workers, and inference.
- Apply queue backpressure so traffic spikes cannot create an unbounded model bill.
- Set timeouts, retries with jitter, dead-letter handling, cancellation, and idempotency for every asynchronous boundary.
- Use database connection pooling compatible with autoscaling and cap maximum connections.
- Define recovery objectives, backup retention, restore tests, and regional limitations.
- Establish budgets and alerts before a live deployment. Tag resources and document scale-to-zero exceptions.
- Pin containers by digest for promotion, keep infrastructure plans as CI artifacts, and require approval for production apply.
- Verify the deployed service with contract, authorization, observability, rollback, and failure-injection smoke tests.

## CI/CD stages

1. Lint, type check, unit test, and contract test.
2. Scan secrets, dependencies, source, container, and Terraform.
3. Build once and publish an immutable image with provenance metadata.
4. Generate and review a Terraform plan using federated identity.
5. Deploy to a test environment, run smoke and security checks, then promote the same artifact.
6. Verify health, SLO signals, and rollback behavior after deployment.
