# Local testing on a clean computer

This guide verifies the submitted agent and produces local results for Tasks 4,
5, and 6. Run every command from the repository root unless a step says
otherwise. Google Cloud credentials are not required.

## 1. Prepare the computer

Use macOS, Linux, or WSL2 on Windows. The shell scripts require a POSIX shell.
Install these tools before cloning the repository:

- Git;
- Python 3.12;
- uv;
- Make;
- curl, jq, and OpenSSL.

Check the tools:

```bash
git --version
uv --version
make --version
curl --version
jq --version
openssl version
```

uv can install a private Python 3.12 runtime if the computer does not already
have one:

```bash
uv python install 3.12
```

## 2. Clone the repository and install the locked workspace

```bash
git clone https://github.com/BlehMaks/siemens-senior-ai-engineer-test.git
cd siemens-senior-ai-engineer-test
```

Use a dedicated ignored environment for this verification. Re-export
`UV_PROJECT_ENVIRONMENT` after opening a new terminal.

```bash
export UV_PROJECT_ENVIRONMENT="$PWD/.local/verification-venv"
uv sync --locked --all-packages --all-groups
```

The command must finish without changing `uv.lock`.

## 3. Add the private source tables

The repository ignores the entire `input/` directory. Copy the assignment files
into this layout:

```text
input/
  task4/
    Training_part1.csv
    Training_part2.csv
  task5/
    Fuse.csv
```

The Task 4 files must keep their original names. All three files use semicolon
delimiters. Task 5 requires the `PART_ID` and `PART_DESCRIPTION` columns.

Export the locations:

```bash
export SIEMENS_TASK4_INPUT_DIR="$PWD/input/task4"
export SIEMENS_FUSE_CSV="$PWD/input/task5/Fuse.csv"
```

Check that the shell can see them:

```bash
test -f "$SIEMENS_TASK4_INPUT_DIR/Training_part1.csv"
test -f "$SIEMENS_TASK4_INPUT_DIR/Training_part2.csv"
test -f "$SIEMENS_FUSE_CSV"
```

Each `test` command must exit silently with status 0.

## 4. Run the complete submission gate

```bash
make local-submission
```

This command creates its own temporary environment, installs the locked
workspace, and runs formatting, lint, strict typing, all public tests, the
submission audit, and the local Tasks 1 to 3 API smoke test. Because both input
variables are set, it also runs the data-dependent Task 4 and Task 5 tests.

The final lines should include:

```text
local acceptance passed without an external LLM engine
local submission check passed for Tasks 1 through 6
```

Do not continue to result generation if this command fails. The first error in
the output is usually the useful one.

## 5. Inspect the submitted agent

### Check the API path

The complete gate already runs this test, but it can also be repeated by itself:

```bash
make local-acceptance
```

The script starts the FastAPI service on a loopback port, creates two tenant
credentials, and checks health, authentication, tenant isolation, run creation,
SSE events, cancellation, and deletion. It stops the service and removes its
temporary database when the test ends.

Expected result:

```text
API smoke passed: health, auth, run, SSE, cancel, tenant isolation, and deletion
local acceptance passed without an external LLM engine
```

If port 8091 is occupied, choose another loopback port:

```bash
LOCAL_ACCEPTANCE_PORT=8092 make local-acceptance
```

### Save and inspect one agent result

```bash
mkdir -p artifacts/local

uv run --frozen --all-packages python -m search_agent.cli \
  "Summarize the documented Siemens sustainability evidence." \
  > artifacts/local/agent-result.json

jq '{
  status: .snapshot.status,
  answer: .snapshot.answer.answer_text,
  citations: .snapshot.answer.citations,
  usage: .usage
}' artifacts/local/agent-result.json
```

`status` must be `completed`. The answer must contain a citation, and the usage
object must report the bounded search and model-call counters.

The submitted acceptance path uses deterministic inference and deterministic
search evidence. It exercises the state machine, budgets, citation contract,
durable API flow, and failure boundaries without downloading a model. The Ollama
and live-search adapters and the CLI live mode are implemented and contract-tested;
selecting the external providers and accepting their live output remain an explicit
owner-run check. This test therefore proves the submitted agent path, not the
quality of an external LLM or a live search provider.

## 6. Train and evaluate Task 4

Create an ignored output directory, then run the seed-42 experiment:

```bash
mkdir -p artifacts/local/task4

uv run --frozen --all-packages python -m binary_classification.evaluate \
  --part1 "$SIEMENS_TASK4_INPUT_DIR/Training_part1.csv" \
  --part2 "$SIEMENS_TASK4_INPUT_DIR/Training_part2.csv" \
  --output-dir artifacts/local/task4 \
  --seed 42
```

The command prints the selected model, threshold, and holdout PR-AUC. It writes:

```text
artifacts/local/task4/metrics.json
artifacts/local/task4/selected-model.pkl
```

Inspect the main result:

```bash
jq '{
  selected_model,
  selected_threshold,
  holdout: .holdout_at_selected_threshold,
  join_audit
}' artifacts/local/task4/metrics.json

test -s artifacts/local/task4/selected-model.pkl
```

For the supplied reference tables and seed 42, the recorded run selected
`weighted_logistic`. Its holdout PR-AUC is about `0.4230`, recall is about
`0.8545`, and precision is about `0.4234`. Small floating-point differences are
acceptable. A different row count, schema, delimiter, or source table is not.

Run the focused test suite:

```bash
uv run --frozen pytest -q task-04-binary-classification/tests
```

## 7. Generate alternatives for Task 5

The lexical model calculates alternatives for the complete catalog in one run:

```bash
mkdir -p artifacts/local/task5

uv run --frozen --all-packages material-similarity \
  "$SIEMENS_FUSE_CSV" \
  --output artifacts/local/task5/alternatives.json
```

The first import of pandas and scikit-learn can take some time. On the reference
catalog, the retrieval calculation targets at most 60 seconds and 1 GB of memory.

Summarize the statuses:

```bash
jq '
  group_by(.status)
  | map({status: .[0].status, count: length})
' artifacts/local/task5/alternatives.json
```

Inspect the first successful recommendation:

```bash
jq 'map(select(.status == "ok"))[0]' \
  artifacts/local/task5/alternatives.json
```

Every result for the supplied catalog must be `ok`, contain five unique alternatives,
and must not return the query part itself. Check that contract:

```bash
jq '[
  .[]
  | . as $result
  | select(.status == "ok")
  | select(
      (.alternatives | length) != 5
      or ([.alternatives[].part_id] | unique | length) != 5
      or ([$result.alternatives[].part_id] | index($result.part_id)) != null
    )
] | length' artifacts/local/task5/alternatives.json
```

The expected output is `0`. Rows with blank descriptions use
`structured_fallback`; inspect `confidence` and `shared_fields`. Six completely
empty source rows are labeled `missingness_only`, not silently presented as
engineering-approved replacements. Use `--mode text` to reproduce the reviewed
description-only abstention behavior.

To inspect one known part separately, replace `<PART_ID>` with an ID from
`Fuse.csv`:

```bash
uv run --frozen --all-packages material-similarity \
  "$SIEMENS_FUSE_CSV" \
  --part-id "<PART_ID>" \
  --output artifacts/local/task5/one-result.json

jq '.' artifacts/local/task5/one-result.json
```

Run the versioned relevance benchmark:

```bash
uv run --frozen --all-packages python -m material_similarity.evaluation \
  "$SIEMENS_FUSE_CSV" \
  task-05-material-similarity/evals/relevance.yaml \
  --output artifacts/local/task5/relevance-metrics.json

jq '{
  selected_word_weight,
  selected_character_weight,
  stability
}' artifacts/local/task5/relevance-metrics.json
```

The benchmark validates the catalog row count and SHA-256 digest. A digest error
means that `Fuse.csv` is not the source catalog used to create the reviewed
benchmark.

Run the focused test suite:

```bash
uv run --frozen pytest -q task-05-material-similarity/tests
```

## 8. Produce a Task 6 result

Task 6 is a Python library, not a CLI application. The following example fits the
mapping on training values, applies the frozen mapping to inference values, and
writes the result to JSON:

```bash
mkdir -p artifacts/local/task6

uv run --frozen --all-packages python - <<'PY'
import json
from pathlib import Path

from category_consolidation import RareCategoryConsolidator

training = ["red", "red", "blue", "green"]
inference = ["red", "yellow", "blue", "cyan"]

model = RareCategoryConsolidator(threshold_percent=50.0)
training_result = model.fit_transform(training)
inference_result = model.transform_with_diagnostics(inference)

payload = {
    "training_input": training,
    "training_output": training_result.values,
    "inference_input": inference,
    "inference_output": inference_result.values,
    "unseen_indexes": list(inference_result.diagnostics.unseen_indexes),
    "unseen_values": list(inference_result.diagnostics.unseen_values),
    "retained_categories": sorted(model.retained_categories),
    "fallback": model.resolved_rare_label,
}

output = Path("artifacts/local/task6/result.json")
output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(output)
PY

jq '.' artifacts/local/task6/result.json
```

Replace the `training` and `inference` lists in the example with the categorical
values you want to process.

At a 50 percent threshold, `red` stays because it is exactly on the boundary.
`blue` and `green` are grouped into `__RARE__`. The unseen inference values
`yellow` and `cyan` also map to the fitted fallback and appear in diagnostics.

Run the focused test suite:

```bash
uv run --frozen pytest -q task-06-category-consolidation/tests
```

## 9. Collect the local results

The generated files are under `artifacts/local/`, which Git ignores:

```text
artifacts/local/
  agent-result.json
  task4/
    metrics.json
    selected-model.pkl
  task5/
    alternatives.json
    one-result.json          # Present after the optional single-part command
    relevance-metrics.json
  task6/
    result.json
```

Confirm that source tables and generated results are not staged:

```bash
git status --short
```

Files under `input/`, `artifacts/`, and `.local/` should not appear in the output.
