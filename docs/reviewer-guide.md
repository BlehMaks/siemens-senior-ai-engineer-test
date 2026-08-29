# Reviewer guide

The shortest useful review starts with the repository contract, then follows one
request through the agent, API, and cloud boundary.

## Ten-minute path

1. Read the [root README](../README.md) for the local and cloud scope.
2. Read [architecture.md](architecture.md) for the Tasks 1 to 3 request path.
3. Run `make local-submission` from a clean clone.
4. Inspect the focused evidence map below.
5. Check the [cloud resource and IAM manifest](cloud-resource-manifest.md).
6. Read the [Task 3 strategy](../task-03-deployment-strategy/architecture/strategy.md)
   before treating assessment defaults as an enterprise recommendation.

## Evidence map

| Claim | Primary implementation | Executable evidence |
|---|---|---|
| Research is bounded by explicit query, fetch, byte, token, and time budgets | `task-01-search-agent/src/search_agent/contracts.py`, `runner.py` | `task-01-search-agent/tests/test_runner.py`, `test_planning.py` |
| Public web content is untrusted and URL access blocks private or metadata targets | `task-01-search-agent/src/search_agent/security/`, `tools/fetch.py` | `task-01-search-agent/tests/security/`, `tools/test_fetch.py` |
| Answers must point to validated evidence | `task-01-search-agent/src/search_agent/evidence.py`, `answering.py` | `task-01-search-agent/tests/test_evidence.py`, `test_answering.py` |
| API admission is asynchronous, idempotent, tenant scoped, and quota aware | `task-02-agent-api/src/agent_api/routes/`, `services/`, `security/` | `task-02-agent-api/tests/routes/`, `security/`, `services/` |
| Local and cloud storage and queue adapters share the same application ports | `task-02-agent-api/src/agent_api/ports.py`, `storage/` | `task-02-agent-api/tests/storage/`, `workers/` |
| Cloud resources and GitHub delivery settings are provisioned through Terraform | `task-03-deployment-strategy/terraform/`, `scripts/bootstrap.sh` | Terraform `.tftest.hcl` files and `task-03-deployment-strategy/tests/test_terraform_bootstrap.py` |
| Deployment uses short-lived identity and immutable image digests | `.github/workflows/deploy.yml`, `terraform/bootstrap/github.tf` | `task-03-deployment-strategy/tests/test_workflows.py` |
| Cloud security state is shared across replicas and Firestore transactions stay bounded | `task-02-agent-api/src/agent_api/security/cloud_state.py`, `storage/cloud.py` | `task-02-agent-api/tests/storage/test_cloud_adapters.py`, `test_run_generation.py` |
| All six tasks can be checked on another computer | `scripts/local_submission_check.sh` | `tests/test_local_submission_script.py` and the command itself |

## Data-science packages

Task 4 and Task 5 reports are committed, but the original assignment tables are
not. Add them locally and use the environment variables shown in
[release and operations](release-and-operations.md) to reproduce the
data-dependent checks. Task 6 is fully deterministic and has no external input.

## Claims to avoid

The checked-in capacity sample is a local fake-provider proof, not an enterprise
load result. The larger multi-cell, GKE, Apigee, Spanner, Vertex AI, corporate IdP,
VPC Service Controls, SIEM, and GPU paths are design options with explicit entry
gates. They have not been provisioned by the assessment Terraform.

Similarly, a successful Terraform plan proves provider compatibility and intended
changes. A successful deployment and smoke run are separate pieces of evidence and
should be recorded with their workflow run, commit, image digest, service revision,
and timestamp.
