# Release and operations

This guide covers two independent outcomes:

1. verify all six assignment solutions on a clean computer;
2. provision and deploy only the Tasks 1 to 3 agent service to Google Cloud.

The cloud path never deploys the Task 4 classifier, Task 5 retrieval package, or
Task 6 consolidation library. The LLM engine also stays outside the deployment.

## Clean-computer verification for Tasks 1 to 6

Install Python 3.12, Git, and uv. Then clone the repository and run:

```bash
git clone https://github.com/BlehMaks/siemens-senior-ai-engineer-test.git
cd siemens-senior-ai-engineer-test
make local-submission
```

The command uses a temporary virtual environment and leaves the checkout clean.
It checks the lock file, formatting, lint, strict typing, all public tests, and
the submission boundary. It then starts the Tasks 1 to 3 API with fake inference
and runs an HTTP smoke scenario with two isolated tenants.

### Add the assignment tables

Task 4 expects this local directory layout:

```text
/absolute/path/to/task4-input/
  Training_part1.csv
  Training_part2.csv
```

Task 5 expects the original `Fuse.csv`. Export both locations before running the
same command:

```bash
export SIEMENS_TASK4_INPUT_DIR=/absolute/path/to/task4-input
export SIEMENS_FUSE_CSV=/absolute/path/to/Fuse.csv
make local-submission
```

The script fails early when an exported path is incomplete. When no private data
is supplied, three data-dependent tests skip with a reason; the remaining public
suite still covers all six packages.

## Cloud prerequisites for Tasks 1 to 3

The operator computer needs:

- Terraform 1.9.8;
- Google Cloud CLI authenticated to the existing project;
- GitHub CLI authenticated to the target repository;
- `git`, `jq`, and `openssl`;
- the current `master` commit pushed before deployment dispatch.

Confirm the two authenticated accounts without changing either platform:

```bash
gcloud auth list
gcloud config get-value project
gh auth status
gh repo view BlehMaks/siemens-senior-ai-engineer-test
```

The project and its billing relationship must already exist. Everything inside
the project, plus the repository delivery boundary, is managed by Terraform.
Read the [cloud resource and IAM manifest](cloud-resource-manifest.md) before the
first apply. It names the required operator access, every default entity, and
every role Terraform will grant.

The repository must already contain `master`. Terraform protects it against
deletion, force pushes, and non-linear history for every actor. The rule does not
require a second reviewer, so a single-person assessment repository remains
usable; GitHub Actions still runs all checks on every push and pull request.

The linked billing account must use EUR. The wrapper checks that requirement and
the alert mailbox before Terraform creates anything. It rejects service-account
mailboxes and addresses without a complete domain.

## What the bootstrap manages

The operator runs one wrapper, but Terraform remains the provisioning engine.
The first apply proceeds in this order:

```text
state_bucket root
  -> separate private, versioned GCS buckets for bootstrap and application state
bootstrap root using the privileged backend
  -> APIs, service accounts, scoped IAM, WIF
  -> GitHub gcp-dev Environment and variables
  -> Secret Manager containers and initial random versions
  -> authenticated Cloud Tasks queue
deploy.yml
  -> tested image, Artifact Registry, named sai-dev Firestore database
  -> Cloud Run API and worker, monitoring and budget resources
```

Secret values are generated during the Terraform apply by a local provisioner.
They are sent to `gcloud secrets versions add` over stdin, so the payloads do not
appear in Terraform state, the process arguments, or GitHub variables.

The assessment never adopts the project's `(default)` Firestore database. It
creates `sai-dev` in `europe-west3`, passes that name to both Cloud Run services,
and conditions runtime data and deployer index roles on that database. Existing
project workloads keep their own database and IAM boundary.

## Plan, apply, verify, and deploy

Point the wrapper at the exact Terraform binary if it is not on `PATH`:

```bash
export TERRAFORM_BIN=/absolute/path/to/terraform
```

Review the first plan:

```bash
./task-03-deployment-strategy/scripts/bootstrap.sh plan \
  liquidity-planning-platform \
  BlehMaks/siemens-senior-ai-engineer-test \
  BlehMaks \
  europe-west3
```

On a new project, the first plan contains only the two state buckets. The
remaining bootstrap plan becomes available after Terraform creates them. Apply
the bootstrap without starting an application deployment:

```bash
./task-03-deployment-strategy/scripts/bootstrap.sh apply \
  liquidity-planning-platform \
  BlehMaks/siemens-senior-ai-engineer-test \
  BlehMaks \
  europe-west3
```

The apply ends with a no-drift verification and checks the Terraform outputs,
GitHub delivery variables, Cloud Tasks queue, and secret versions. It is safe to
rerun after an interruption: existing secret versions are kept, remote state is
reused, and a legacy combined state bucket is migrated before Terraform plans
against the separated backends.

After the exact local commit is present on remote `master`, provision and start
the protected delivery workflow with:

```bash
./task-03-deployment-strategy/scripts/bootstrap.sh deploy \
  liquidity-planning-platform \
  BlehMaks/siemens-senior-ai-engineer-test \
  BlehMaks \
  europe-west3
```

The wrapper does not call `gcloud run deploy` or create cloud resources directly.
It applies Terraform, verifies the result, compares local and remote revisions,
then dispatches `.github/workflows/deploy.yml`. Each dispatch gets a random
correlation ID and the verified commit SHA. The first workflow job compares that
SHA with GitHub's resolved `master`; if the branch moved, the correlated run
fails before tests, credentials, or deployment. The wrapper watches that exact
run and reports the failure instead of following another revision.

The wrapper reads the project's linked billing account and defaults the alert
recipient to the active human `gcloud` account. The dev stack creates a EUR 5
budget with early alerts. Both Cloud Run services scale to zero and have a
one-instance maximum; the queue accepts one concurrent delivery. After CI
finishes, the wrapper reads the applied Terraform state and refuses success if
those limits are absent. A Google Cloud budget sends alerts but does not stop
charges by itself.

## CI/CD path

Pull requests and pushes to `master` run unprivileged checks. A deployment is
manual and uses the Terraform-managed `gcp-dev` Environment. GitHub exchanges its
OIDC token for a short-lived deployer credential bound to the repository ID,
branch, and environment. No service-account key is stored in GitHub.

The deployment workflow:

1. rebuilds, tests, scans, and exports the image;
2. pushes the exact tested image and resolves its immutable digest;
3. creates a Terraform binary plan bound to the Git commit and digest;
4. exposes that plan for the protected apply job;
5. applies only the uploaded plan and reports the ready Cloud Run revisions.

## Verification and recovery

Check the bootstrap at any time:

```bash
./task-03-deployment-strategy/scripts/bootstrap.sh verify \
  liquidity-planning-platform \
  BlehMaks/siemens-senior-ai-engineer-test \
  BlehMaks \
  europe-west3
```

Inspect the GitHub workflow result with `gh run list` and `gh run view`. The
assessment-cell smoke, rollback, and guarded teardown commands are documented in
[Task 3 runbooks](../task-03-deployment-strategy/architecture/runbooks.md).

A runtime rollback switches Cloud Run traffic to an existing healthy revision.
It does not rebuild an old commit or change Git history. Destruction is a separate
reviewed action and is never part of the bootstrap wrapper.

## External decisions that remain manual

Terraform cannot create the user's billing account, accept platform terms, pass
MFA, or approve its own protected GitHub Environment. Those account-level actions
remain with a human operator. Corporate identity, data residency, retention,
production SLOs, model licensing, and final capacity are enterprise design inputs,
not claims made by this assessment deployment.
