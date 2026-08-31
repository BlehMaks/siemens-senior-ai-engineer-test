# Owner acceptance checklist

Use this checklist on the delivery computer and again on a second clean computer.
Run commands from the repository root unless stated otherwise.

Status labels:

- **Agent-validated** means the command, path, and expected local behavior are
  covered by repository tests or were exercised in the development checkout. It
  does not claim a second-computer, private-data, or live-cloud pass.
- **Owner-only** requires the repository owner because it uses private assignment
  files, another computer, billing-enabled cloud access, protected environments,
  or business interpretation.

Record the acceptance context before starting:

```text
Date/time:
Computer and OS:
Git commit:
Python version:
uv version:
Owner/reviewer:
```

## 1. Clone and install the locked workspace

**Owner-only: second-computer execution. Agent-validated: command syntax and locked
workspace contract.**

The clone and first dependency sync are network operations. They require access to
GitHub and the configured Python package index or a complete local package cache.
No cloud account, private dataset, or model download is required.

```bash
git clone https://github.com/BlehMaks/siemens-senior-ai-engineer-test.git
cd siemens-senior-ai-engineer-test
uv python install 3.12
uv sync --locked --all-packages --all-groups
uv lock --check
git status --short
```

Acceptance:

- [ ] `uv lock --check` exits with status 0.
- [ ] `git status --short` prints nothing; installation did not change tracked
  files or `uv.lock`.
- [ ] The checkout is the intended revision: `git rev-parse HEAD` matches the
  recorded delivery commit.

If the sync cannot reach the package index, restore network access or the approved
package cache. Do not regenerate `uv.lock`. See
[troubleshooting](troubleshooting.md) for Python and lock failures.

After a successful sync, the deterministic gates below need no internet, cloud
account, private data, or external model. `make local-submission` creates another
temporary environment and may need the same package cache or network access.

## 2. Run deterministic repository gates

**Agent-validated. Owner-only: repeat on the second computer.**

```bash
make check
make coverage-report
make local-submission
```

`make check` verifies the lock, formatting, lint, strict typing, deterministic
tests, English-only text, local Markdown links, and the submission boundary.
`make coverage-report` is the diagnostic line-and-branch report. A nonzero exit from
any command is a failure; do not continue by skipping its first failing gate.

The final successful `make local-submission` output includes:

```text
local acceptance passed without an external LLM engine
local submission check passed for Tasks 1 through 6
```

Acceptance:

- [ ] All three commands exit with status 0.
- [ ] `make local-submission` prints both success lines above.
- [ ] No live search, external LLM, private dataset, or cloud deployment was
  presented as part of this deterministic pass.

Representative failures are explicit. A bad optional Task 4 path prints, for
example:

```text
error: SIEMENS_TASK4_INPUT_DIR is missing Training_part1.csv
```

The language, link, and submission gates print the affected path when they fail.
Fix the source or input location; never weaken an audit to obtain a pass.

## 3. Configure private Task 4 and Task 5 inputs

**Owner-only.** Keep all three source tables outside Git and do not paste their rows
into issues, reports, or review messages.

macOS/Linux example:

```bash
export SIEMENS_TASK4_INPUT_DIR="/path/to/private/task4"
export SIEMENS_FUSE_CSV="/path/to/private/Fuse.csv"
test -f "$SIEMENS_TASK4_INPUT_DIR/Training_part1.csv"
test -f "$SIEMENS_TASK4_INPUT_DIR/Training_part2.csv"
test -f "$SIEMENS_FUSE_CSV"
```

Each `test` command succeeds silently. The Task 4 filenames are fixed and both
datasets must retain their supplied semicolon-delimited schema.

On Windows, run the repository commands inside WSL2. Quote every converted path,
especially when a Windows folder contains spaces:

```bash
export SIEMENS_TASK4_INPUT_DIR="/mnt/c/Users/Owner/Siemens input/task4"
export SIEMENS_FUSE_CSV="/mnt/c/Users/Owner/Siemens input/Fuse.csv"
test -f "$SIEMENS_TASK4_INPUT_DIR/Training_part1.csv"
test -f "$SIEMENS_TASK4_INPUT_DIR/Training_part2.csv"
test -f "$SIEMENS_FUSE_CSV"
```

Replace `Owner` and the illustrative folders with the actual WSL paths. Do not use
unquoted `C:\...` paths in the POSIX shell.

Acceptance:

- [ ] All three files are readable through the exported variables.
- [ ] `git status --short` does not list a private input.
- [ ] `make local-submission` now runs the private-data tests instead of reporting
  their documented skips.

A delimiter, schema, row-count, duplicate-ID, or fingerprint error means the file
is not the reviewed source table. Do not suppress that validation.

## 4. Generate and inspect the Task 4 full-data report

**Owner-only: private data, cost interpretation, and result acceptance.** Bundled
cost scenarios are examples, not Siemens business truth.

```bash
mkdir -p artifacts/local/task4-extension

uv run --frozen --all-packages python -m binary_classification.evaluate \
  --part1 "$SIEMENS_TASK4_INPUT_DIR/Training_part1.csv" \
  --part2 "$SIEMENS_TASK4_INPUT_DIR/Training_part2.csv" \
  --output-dir artifacts/local/task4-extension \
  --seed 42 \
  --cost-scenario balanced-review \
  --cost-scenario miss-averse-review

test -s artifacts/local/task4-extension/selected-model.pkl
test -s artifacts/local/task4-extension/baseline-vs-extension.json
test -s artifacts/local/task4-extension/baseline-vs-extension.md

jq '{
  schema_version,
  metadata: {
    dataset_fingerprint: .metadata.dataset_fingerprint,
    row_counts: .metadata.row_counts,
    seed: .metadata.seed
  },
  baseline_status: .assignment_baseline.mode_status,
  extension_status: .business_extension.mode_status,
  scenarios: [.business_extension.decision_scenarios[].scenario.name]
}' artifacts/local/task4-extension/baseline-vs-extension.json
```

The training command prints a line beginning with `selected=` and writes the JSON
and Markdown from one result object. Acceptance:

- [ ] Both modes report their real status and neither is silently substituted for
  the other.
- [ ] The JSON contains only aggregate evidence; no private row appears.
- [ ] Dataset fingerprint, row counts, and seed match the intended run.
- [ ] The owner records whether example costs are acceptable or supplies a reviewed
  `--cost-config` without describing examples as confirmed business costs.
- [ ] Artifact round-trip parity and holdout limitations remain visible.

Invalid or non-finite costs, unexpected labels, schema failures, or a missing
output file are blocking failures.

## 5. Generate and inspect the Task 5 full-data report

**Owner-only: private catalog, relevance review, and engineering compatibility
interpretation.** Results are candidates for review, never approved substitutes.

```bash
mkdir -p artifacts/local/task5-extension

uv run --frozen --all-packages python -m material_similarity.evaluation \
  "$SIEMENS_FUSE_CSV" \
  task-05-material-similarity/evals/relevance.yaml \
  --mode comparison \
  --safety-benchmark task-05-material-similarity/evals/safety.yaml \
  --output artifacts/local/task5-extension/baseline-vs-extension.json \
  --markdown-output artifacts/local/task5-extension/baseline-vs-extension.md

test -s artifacts/local/task5-extension/baseline-vs-extension.json
test -s artifacts/local/task5-extension/baseline-vs-extension.md

jq '{
  schema_version,
  dataset_fingerprint: .metadata.dataset_fingerprint,
  row_count: .metadata.row_count,
  baseline_status: .assignment_baseline.mode_status,
  extension_status: .business_extension.mode_status,
  relaxed_hybrid: .business_extension.relaxed_hybrid.mode_status,
  status_counts: .business_extension.status_counts,
  review_workload: .business_extension.review_workload
}' artifacts/local/task5-extension/baseline-vs-extension.json
```

Acceptance:

- [ ] JSON and Markdown exist and describe the same run and dataset fingerprint.
- [ ] Lexical baseline, strict hybrid, and structured-only results remain visibly
  separate.
- [ ] Blank descriptions never enter the text model.
- [ ] Hard conflicts cannot be overridden by a similarity score.
- [ ] `review_required` and `insufficient_evidence` remain honest outcomes.
- [ ] Relaxed hybrid is reported as `evaluated`, remains separate from the
  lexical baseline and strict hybrid, and emits review candidates rather than
  approved substitutes.
- [ ] The owner reviews representative candidates and records that similarity and
  benchmark labels are not electrical interchangeability certification.

A command-line error requiring `--output`, `--markdown-output`, or
`--safety-benchmark`, or any parser/schema failure, is blocking rather than a
reason to manufacture missing metrics.

## 6. Generate and inspect the Task 6 report

**Agent-validated. Owner-only: threshold-policy acceptance for a real downstream
use.** Task 6 uses a deterministic sanitized fixture and no private input.

```bash
mkdir -p artifacts/local/task6

uv run --frozen --all-packages python -m category_consolidation.evaluation \
  --output-dir artifacts/local/task6

test -s artifacts/local/task6/baseline-vs-extension.json
test -s artifacts/local/task6/baseline-vs-extension.md

jq '{
  schema_version,
  baseline_equivalent: .assignment_baseline.single_column_output_equivalent,
  extension_status: .business_extension.mode_status,
  artifact: .business_extension.artifact,
  sklearn_checks: .business_extension.sklearn_checks,
  aliases: .business_extension.alias_normalization.mode_status
}' artifacts/local/task6/baseline-vs-extension.json
```

The command prints two `Wrote ...` lines. Acceptance:

- [ ] `baseline_equivalent` is `true`.
- [ ] All recorded sklearn checks are `true`.
- [ ] The artifact has schema version `1` and a `sha256:` fingerprint.
- [ ] Alias normalization is `evaluated`, disabled by default, maps only declared
  aliases, and leaves undeclared variants subject to the normal unseen-category
  fallback.
- [ ] Runtime/memory values are treated as bounded local evidence, not a universal
  performance promise.

## 7. Inspect outputs and preserve the submission boundary

**Owner-only for private-data outputs.**

```bash
git status --short
uv run --frozen python scripts/audit_submission.py
uv run --frozen python scripts/audit_language.py
uv run --frozen python scripts/audit_links.py
```

Expected successful audit lines:

```text
Submission audit passed.
Language audit passed.
Documentation link audit passed.
```

Keep run outputs under ignored `artifacts/local/` or another owner-controlled path.
Do not add private reports, CSV files, credentials, model artifacts, temporary
environments, or reviewer reproduction files to a delivery commit.

Acceptance:

- [ ] Only intended source, test, documentation, and sanitized fixture evidence is
  tracked.
- [ ] Private outputs remain local and access-controlled.
- [ ] The clean delivery commit passes all three audits.

## 8. Owner-only cloud and operational acceptance

Tasks 4–6 are local packages and are not deployed. Live Tasks 1–3 deployment and
cloud smoke testing require billing, authenticated Google Cloud access, protected
GitHub environment approval, and the exact reviewed commit. Follow
[release and operations](release-and-operations.md); do not infer a successful
deployment from a local test or Terraform plan.

- [ ] Record the GitHub workflow run, commit, immutable image digest, Cloud Run
  revision, region, and timestamp.
- [ ] Confirm traffic and health against the deployed revision.
- [ ] Confirm no fallback deployment changed Git history or rebuilt an old revision
  without explicit approval.
- [ ] Record any owner-found setup, private-data, business-policy, or deployment
  defect against the smallest responsible task.

This section cannot be marked agent-validated. A Terraform plan proves intended
provider changes only; it is not a deployment or production-readiness claim.

## 9. Final owner sign-off

- [ ] Second-computer clone and locked install passed.
- [ ] Deterministic gates passed at the recorded commit.
- [ ] Private Task 4 and Task 5 checks and reports passed.
- [ ] Task 6 sanitized report passed.
- [ ] Baseline and extension claims remain separate in all three reports.
- [ ] Private data and local artifacts remain outside Git.
- [ ] Live-cloud acceptance is either recorded with evidence or explicitly marked
  not run.
- [ ] Remaining limitations and owner decisions are recorded without converting an
  untested assumption into a pass.

Owner signature/date: ________________________________________________
