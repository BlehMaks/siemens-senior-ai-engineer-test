# Getting started

## Prerequisites

Use macOS, Linux, or Windows with WSL2 and a POSIX shell. Install Git, Make, and
`uv`. The workspace requires Python 3.12; `uv python install 3.12` can install an
isolated runtime. No cloud account, model download, or private dataset is required
for the deterministic public tests.

## Clean checkout

```bash
git clone https://github.com/BlehMaks/siemens-senior-ai-engineer-test.git
cd siemens-senior-ai-engineer-test
uv python install 3.12
uv sync --locked --all-packages --all-groups
uv lock --check
make check
```

The locked sync must not modify `uv.lock`. `make check` runs formatting, lint,
strict typing, deterministic tests, the English-only audit, and the submission
boundary audit. Network, live-cloud, and external-model behavior are outside that
gate.

## Private Task 4 and Task 5 inputs

Keep private inputs in an ignored directory or elsewhere on the computer. Never
add them to Git. Point commands at absolute local paths:

```bash
export SIEMENS_TASK4_INPUT_DIR="/path/to/task4"
export SIEMENS_FUSE_CSV="/path/to/Fuse.csv"
test -f "$SIEMENS_TASK4_INPUT_DIR/Training_part1.csv"
test -f "$SIEMENS_TASK4_INPUT_DIR/Training_part2.csv"
test -f "$SIEMENS_FUSE_CSV"
make local-submission
```

On Windows, run these commands inside WSL2 and quote paths that contain spaces.
Access Windows files as `/mnt/c/...`, or copy them into an ignored WSL directory.

## First results

Baseline commands appear before opt-in extension commands in each task README:

- [Task 4 binary classification](../task-04-binary-classification/README.md)
- [Task 5 material similarity](../task-05-material-similarity/README.md)
- [Task 6 category consolidation](../task-06-category-consolidation/README.md)

Use `artifacts/local/` for disposable runs. It is ignored. To remove local results
and the normal workspace environment without touching source data:

```bash
rm -rf artifacts/local .venv
```

The removal command is optional and must be run only from the repository root.
For the full clean-computer checklist and expected output, continue with
[local testing](local-testing.md). For failures, see
[troubleshooting](troubleshooting.md).
