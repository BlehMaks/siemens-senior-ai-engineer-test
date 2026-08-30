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
- Google Application Default Credentials for an operator who can read and
  bootstrap `siemens-senior-ai-engineer`;
- GitHub CLI authenticated to the target repository;
- `git`, `jq`, and `openssl`;
- the current `master` commit pushed before deployment dispatch.

Confirm GitHub access without changing the repository:

```bash
gh auth status
gh repo view BlehMaks/siemens-senior-ai-engineer-test
```

Terraform cannot complete an interactive account login or MFA challenge. On each
new operator computer, create its local ADC file before the first plan:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project siemens-senior-ai-engineer
```

This is a one-time credential setup, not a provisioning step. The wrapper and the
GitHub deployment workflow do not call `gcloud`. After login, `bootstrap.sh plan`
must read project number `163220015018` and show only the two state buckets on a
clean project. A `data.google_project.current` permission error means that the ADC
identity still lacks access or belongs to a different account.

The project and its billing relationship must already exist. Everything inside
the project, plus the repository delivery boundary, is managed by Terraform.
Read the [cloud resource and IAM manifest](cloud-resource-manifest.md) before the
first apply. It names the required operator access, every default entity, and
every role Terraform will grant.

The repository must already contain `master`. Terraform protects it against
deletion, force pushes, and non-linear history for every actor. The rule does not
require a second reviewer, so a single-person assessment repository remains
usable; GitHub Actions still runs all checks on every push and pull request.

The linked billing account must use EUR. Supply its ID and at least one monitored
human mailbox before planning:

```bash
export GCP_BILLING_ACCOUNT_ID=ABCDEF-123456-ABCDEF
export GCP_BUDGET_NOTIFICATION_EMAILS='["operator@example.com"]'
```

The wrapper validates both values before Terraform creates anything. It rejects
service-account mailboxes and addresses without a complete domain.

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

Terraform generates both initial secret values and writes their Secret Manager
versions. The values are marked sensitive and remain in the protected,
versioned bootstrap state; they never enter command arguments or GitHub variables.

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
  siemens-senior-ai-engineer \
  BlehMaks/siemens-senior-ai-engineer-test \
  BlehMaks \
  europe-west3
```

On a new project, the first plan contains only the two state buckets. The
remaining bootstrap plan becomes available after Terraform creates them. Apply
the bootstrap without starting an application deployment:

```bash
./task-03-deployment-strategy/scripts/bootstrap.sh apply \
  siemens-senior-ai-engineer \
  BlehMaks/siemens-senior-ai-engineer-test \
  BlehMaks \
  europe-west3
```

The apply ends with a no-drift verification and checks the Terraform outputs,
GitHub delivery variables, Cloud Tasks queue, and secret versions. It is safe to
rerun after an interruption: existing secret versions are kept, remote state is
reused, and Terraform refreshes each managed resource before planning. On a new
computer that adopts existing state buckets, set `GCP_IMPORT_STATE_BUCKETS=true`
for the first run.

After the exact local commit is present on remote `master`, provision and start
the protected delivery workflow with:

```bash
./task-03-deployment-strategy/scripts/bootstrap.sh deploy \
  siemens-senior-ai-engineer \
  BlehMaks/siemens-senior-ai-engineer-test \
  BlehMaks \
  europe-west3
```

Neither the wrapper nor the deployment workflow invokes the Google Cloud CLI.
They apply Terraform, verify its outputs, compare local and remote revisions,
then dispatches `.github/workflows/deploy.yml`. Each dispatch gets a random
correlation ID and the verified commit SHA. The first workflow job compares that
SHA with GitHub's resolved `master`; if the branch moved, the correlated run
fails before tests, credentials, or deployment. The wrapper watches that exact
run and reports the failure instead of following another revision.

The wrapper uses the explicit billing account and alert recipients supplied by
the operator. The dev stack creates a EUR 5 budget with early alerts. Both Cloud
Run services scale to zero and have a
one-instance maximum; the queue accepts one concurrent delivery. After CI
finishes, the wrapper reads the applied Terraform state and refuses success if
those limits are absent. A Google Cloud budget sends alerts but does not stop
charges by itself.

Run the bounded cloud smoke after the workflow succeeds:

```bash
./task-03-deployment-strategy/scripts/cloud_api_smoke.sh \
  siemens-senior-ai-engineer europe-west3 dev 163220015018 review-001
```

The script discovers the service URL, creates two temporary tenant keys in
`sai-dev`, exercises the public API and Firestore deletion path, and revokes the
keys. Each key also expires after 15 minutes. The script writes plaintext only
to mode `0600` files in its temporary directory, keeps it out of command
arguments and output, and removes the directory on exit. The exact smoke-only
IAM requirements are listed in the resource manifest.

If a key-file write fails, the CLI clears the file and moves it to a mode `0600`
sibling named `.api-key-cleanup-*`. The requested path remains available for a
safe retry, while any entry created there by another process is preserved. The
smoke script removes its temporary directory when it exits.

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
  siemens-senior-ai-engineer \
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

Terraform starts only after an operator has completed these account-level steps:

1. Create or select `siemens-senior-ai-engineer` and confirm project number
   `163220015018`.
2. Link an active EUR billing account and authorize the operator to use it.
3. Accept required Google Cloud terms, complete MFA, and create local ADC with the
   commands in the prerequisites section.
4. Create the GitHub repository and push `master` once. Terraform can then manage
   its rules, variables, environment, and Workload Identity Federation boundary.
5. Approve a protected GitHub Environment deployment when GitHub asks for human
   review. Terraform cannot approve its own run.

Corporate identity, data residency, retention, production SLOs, model licensing,
and final capacity also require decisions from the owning teams. The assessment
does not claim to settle them.
