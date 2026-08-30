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
7. Use the [owner acceptance checklist](owner-acceptance-checklist.md) for a clean
   second-computer run, private Tasks 4–5 reports, and explicit cloud sign-off.

## Evidence map

| Claim | Primary implementation | Executable evidence |
|---|---|---|
| Research is bounded by explicit query, fetch, byte, token, and time budgets | `task-01-search-agent/src/search_agent/contracts.py`, `runner.py` | `task-01-search-agent/tests/test_runner.py`, `test_planning.py` |
| Public web content is untrusted and URL access blocks private or metadata targets | `task-01-search-agent/src/search_agent/security/`, `tools/fetch.py` | `task-01-search-agent/tests/security/`, `tools/test_fetch.py` |
| HTML/text/PDF sources become structural chunks and only ranked bounded context reaches synthesis | `task-01-search-agent/src/search_agent/tools/extract.py`, `documents.py`, `retrieval.py`, `runner.py` | `task-01-search-agent/tests/tools/test_extract.py`, `test_retrieval.py`, `test_runner.py` |
| Search uses typed backend outcomes and a bounded `auto`/DuckDuckGo fallback | `task-01-search-agent/src/search_agent/tools/search.py`, `runtime.py`, `cli.py` | `task-01-search-agent/tests/tools/test_search.py`, `test_runtime.py`, `test_cli.py` |
| Answers must point to validated evidence | `task-01-search-agent/src/search_agent/evidence.py`, `answering.py` | `task-01-search-agent/tests/test_evidence.py`, `test_answering.py` |
| Follow-ups receive only bounded completed same-session public context | `task-01-search-agent/src/search_agent/planning.py`, `runtime.py`; `task-02-agent-api/src/agent_api/workers/local.py` | `task-01-search-agent/tests/test_planning.py`; `task-02-agent-api/tests/workers/test_local.py` |
| Every research stage emits privacy-safe action metadata and persists a v2 reflection with v1 read compatibility | `task-01-search-agent/src/search_agent/runner.py`, `memory/`; `task-02-agent-api/src/agent_api/observability.py` | `task-01-search-agent/tests/test_runner.py`, `memory/test_reflection.py`; `task-02-agent-api/tests/test_observability.py` |
| Reviewed memory is loaded automatically for local Ollama runs and its use is exposed truthfully | `task-01-search-agent/src/search_agent/runtime.py`, `runner.py`; `task-02-agent-api/src/agent_api/workers/local.py`, `ui.py` | `task-01-search-agent/tests/test_runner.py`; `task-02-agent-api/tests/workers/test_local.py`, `test_ui.py` |
| API admission is asynchronous, idempotent, tenant scoped, and quota aware | `task-02-agent-api/src/agent_api/routes/`, `services/`, `security/` | `task-02-agent-api/tests/routes/`, `security/`, `services/` |
| Local and cloud storage and queue adapters share the same application ports | `task-02-agent-api/src/agent_api/ports.py`, `storage/` | `task-02-agent-api/tests/storage/`, `workers/` |
| Cloud resources and GitHub delivery settings are provisioned through Terraform | `task-03-deployment-strategy/terraform/`, `scripts/bootstrap.sh` | Terraform `.tftest.hcl` files and `task-03-deployment-strategy/tests/test_terraform_bootstrap.py` |
| The opt-in production root wires a private Cloud Run Ollama service to a real authenticated worker while dev remains fake | `task-03-deployment-strategy/terraform/environments/production/`, `modules/run_services/`, `src/deployment_strategy/container.py` | production/run-services Terraform tests and `task-03-deployment-strategy/tests/test_container.py` |
| Deployment uses short-lived identity and immutable image digests | `.github/workflows/deploy.yml`, `terraform/bootstrap/github.tf` | `task-03-deployment-strategy/tests/test_workflows.py` |
| Cloud security state is shared across replicas and Firestore transactions stay bounded | `task-02-agent-api/src/agent_api/security/cloud_state.py`, `storage/cloud.py` | `task-02-agent-api/tests/storage/test_cloud_adapters.py`, `test_run_generation.py` |
| All six tasks can be checked on another computer | `scripts/local_submission_check.sh` | `tests/test_local_submission_script.py` and the command itself |
| Task 4 preserves grouped baseline evaluation while calibrated cost scenarios remain explicit opt-ins | `task-04-binary-classification/src/binary_classification/evaluate.py`, `calibration.py`, `decision.py` | `task-04-binary-classification/tests/`; `task-04-binary-classification/reports/baseline-vs-extension.json` and `.md` |
| Task 5 keeps lexical retrieval separate from compatibility-gated and structured-only review modes | `task-05-material-similarity/src/material_similarity/hybrid.py`, `evaluation.py` | `task-05-material-similarity/tests/`; `task-05-material-similarity/reports/baseline-vs-extension.json` and `.md` |
| Task 6 retains the dependency-free percentage helper and adds an opt-in multi-column sklearn adapter with safe JSON mappings | `task-06-category-consolidation/src/category_consolidation/core.py`, `sklearn.py`, `artifact.py` | `task-06-category-consolidation/tests/`; `task-06-category-consolidation/reports/baseline-vs-extension.json` and `.md` |

## Data-science packages

Task 4 and Task 5 reports are committed, but the original assignment tables are
not. Add them locally and use the environment variables shown in
[release and operations](release-and-operations.md) to reproduce the
data-dependent checks. Task 4 persists and parity-checks its fitted pipeline;
Task 5 rebuilds one transparent TF-IDF index per catalog invocation and returns five
labeled alternatives for every supplied row; Task 6 is a fitted deterministic
transform, not a separately trained predictive model, and has no external input.
The [owner acceptance checklist](owner-acceptance-checklist.md) keeps locally
reproducible evidence distinct from second-computer, private-data, business-policy,
and live-cloud acceptance that only the repository owner can complete.

## Claims to avoid

The checked-in capacity sample is a local fake-provider proof, not an enterprise
load result. The larger multi-cell, GKE, Apigee, Spanner, Vertex AI, corporate IdP,
VPC Service Controls, SIEM, and GPU paths are design options with explicit entry
gates. They have not been provisioned by the assessment Terraform.

Similarly, a successful Terraform plan proves provider compatibility and intended
changes. A successful deployment and smoke run are separate pieces of evidence and
should be recorded with their workflow run, commit, image digest, service revision,
and timestamp.
