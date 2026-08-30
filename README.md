# Siemens Senior AI Engineer technical assignment

This repository contains all six assignment solutions. Tasks 1 to 3 form one
deployable research-agent service. Tasks 4 to 6 are local data-science packages
and are not part of the cloud runtime.

## Start here

The project uses Python 3.12 and [uv](https://docs.astral.sh/uv/). From a fresh
clone, run the complete local submission check:

```bash
make local-submission
```

That command creates a temporary environment, installs every workspace package,
runs formatting, lint, typing, tests, and the submission audit, then starts the
Tasks 1 to 3 API with deterministic fake inference and exercises its public
contract. It does not need a cloud account or an external LLM.

Task 4 and Task 5 use source tables that are deliberately excluded from Git. To
include their data-dependent checks, add the files locally and export:

```bash
export SIEMENS_TASK4_INPUT_DIR=/absolute/path/to/task4-input
export SIEMENS_FUSE_CSV=/absolute/path/to/Fuse.csv
make local-submission
```

`SIEMENS_TASK4_INPUT_DIR` must contain `Training_part1.csv` and
`Training_part2.csv`. Without these optional inputs, the public suite still
checks all six packages and reports three explicit skips.

To exercise only the running agent and API from Tasks 1 to 3:

```bash
make local-acceptance
```

## Cloud delivery for Tasks 1 to 3

The cloud path provisions the agent, API, worker, storage, queue, secrets,
identity, observability, container registry, and CI/CD boundary. It does not
deploy Tasks 4 to 6 or an LLM engine.

Task 3 also contains a separate production model-plane reference. It is disabled
by default and cannot be enabled by the assessment workflow. The opt-in profile
uses a private, digest-pinned Cloud Run GPU service and has its own cost gate;
it is included as production infrastructure design, not part of this cloud test.

The operator entry point is
[`bootstrap.sh`](task-03-deployment-strategy/scripts/bootstrap.sh). It uses
Terraform for every GCP and GitHub configuration change. A normal first run is:

```bash
# One-time workstation login. bootstrap.sh does not run these commands.
gcloud auth application-default login
gcloud auth application-default set-quota-project siemens-senior-ai-engineer

export TERRAFORM_BIN=/absolute/path/to/terraform

./task-03-deployment-strategy/scripts/bootstrap.sh plan \
  siemens-senior-ai-engineer \
  BlehMaks/siemens-senior-ai-engineer-test \
  BlehMaks \
  europe-west3

./task-03-deployment-strategy/scripts/bootstrap.sh deploy \
  siemens-senior-ai-engineer \
  BlehMaks/siemens-senior-ai-engineer-test \
  BlehMaks \
  europe-west3
```

The login creates Application Default Credentials for the Terraform provider. It
does not provision or change cloud resources. The project, billing link, account
terms, MFA, and any protected-environment approval remain manual prerequisites.
The deployment guide lists each manual step and its expected result.
Successful ADC setup is confirmed when `bootstrap.sh plan` reads project number
`163220015018` and returns the two state buckets without a project-access error.

`deploy` applies the reviewed Terraform bootstrap, verifies it, and then
dispatches the protected GitHub workflow for the exact pushed `master` commit.
The wrapper refuses to dispatch when local and remote revisions differ.

The full clean-machine, provisioning, verification, and recovery procedure is in
[release and operations](docs/release-and-operations.md). The exact resource and
IAM change list is in the [cloud manifest](docs/cloud-resource-manifest.md).

## Deliverables

| Task | Package | Main result |
|---|---|---|
| 1 | [Internet-search agent](task-01-search-agent/README.md) | Bounded research loop with validated evidence, citations, memory, and web safety controls |
| 2 | [Agent API](task-02-agent-api/README.md) | Tenant-isolated asynchronous API with durable runs, SSE, quotas, cancellation, and local or cloud adapters |
| 3 | [Deployment strategy](task-03-deployment-strategy/README.md) | Terraform-managed GCP assessment cell and keyless GitHub delivery path |
| 4 | [Binary classification](task-04-binary-classification/README.md) | Reproducible model analysis and evaluation package |
| 5 | [Material similarity](task-05-material-similarity/README.md) | Alternative-material retrieval and relevance evaluation package |
| 6 | [Category consolidation](task-06-category-consolidation/README.md) | Deterministic rare-category consolidation library |

## Review map

- [Architecture](docs/architecture.md) explains how Tasks 1 to 3 work together.
- [Reviewer guide](docs/reviewer-guide.md) maps claims to code, tests, and reports.
- [Cloud resource and IAM manifest](docs/cloud-resource-manifest.md) lists every
  default entity, role, and GitHub variable before the first apply.
- [Test strategy](docs/test-strategy.md) lists the blocking local gates.
- [Submission boundary](docs/submission-boundary.md) records what is safe to push.
- [Task 3 strategy](task-03-deployment-strategy/architecture/strategy.md) separates
  the small assessment cell from the larger enterprise design.

The source assignment refers once to "all four tasks" while defining six task
sections. This repository follows all six sections and keeps the requested
priority on Tasks 1 to 3.
