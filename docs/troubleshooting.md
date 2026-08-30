# Troubleshooting

## `uv` selects the wrong Python

Run `uv python install 3.12`, then remove only the repository `.venv` and repeat
`uv sync --locked --all-packages --all-groups`. Every task requires Python 3.12.

## Locked sync changes or rejects `uv.lock`

Do not regenerate the lock as a workaround. Confirm that the checkout is clean and
that the installed `uv` can read the lock with `uv lock --check`. A deliberate
dependency change must update `pyproject.toml` and `uv.lock` together.

## Private-data tests skip

This is expected in a public checkout. Set `SIEMENS_TASK4_INPUT_DIR` to the directory
containing both original Task 4 CSV files and `SIEMENS_FUSE_CSV` to the Task 5 CSV.
Keep the files outside Git. A delimiter, filename, schema, row-count, or fingerprint
error means the input is not the reviewed reference table; do not suppress it.

## Optional Task 6 imports fail

The standalone Task 6 API has no pandas or sklearn requirement. Install its explicit
optional integration extra before importing the sklearn adapter. Use the exact
command documented in the Task 6 README so the lock remains authoritative.

## Offline execution fails

`make check` is deterministic after the locked environment is installed. Initial
dependency installation needs access to the configured package cache or index.
Cloud deployment, live search, private-data reports, and external model downloads
are separate opt-in operations and are not implied by a green local gate.

## Submission audit reports a file

Read the reported path before changing anything. Move private data and generated
run artifacts into ignored `input/` or `artifacts/` locations. Never weaken the
audit or commit secrets to make the gate pass.

## English-only audit reports a line

Translate repository source, comments, docs, fixtures, CLI text, diagram sources,
or editable report sources into English. Binary artifacts are skipped because their
editable source is the review boundary; there is no user-facing-text allowlist.
