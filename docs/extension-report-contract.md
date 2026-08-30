# Baseline-versus-extension report contract

Tasks 4–6 implement this small envelope independently. It is a documentation
contract, not a shared runtime package.

Each JSON report contains:

- `schema_version`: a task-owned version string;
- `metadata`: task identifier, deterministic configuration, generation command or
  entry point, durations, package versions, and sanitized data fingerprint;
- `baseline`: unchanged assignment behavior and measurements;
- `extension`: opt-in modes and their measurements;
- `delta`: explicitly named comparable differences, never a claim that unlike
  metrics are equivalent;
- `limitations`: known evidence, data, safety, and interpretation boundaries;
- `mode_status`: `evaluated`, `skipped`, or `not_implemented` for every declared
  optional mode;
- `data_fingerprint`: a digest or stable synthetic-fixture identifier, never raw
  private data.

Objects use sorted keys and stable list ordering. Non-finite numbers, timestamps
that vary between identical deterministic runs, machine-specific absolute paths,
raw rows, source identifiers, and secrets are excluded. A newer reader must reject
an unknown major schema version and may accept documented additive minor fields.

Markdown is rendered from the exact in-memory object written as JSON. It must not
contain independently copied metrics. Baseline and extension sections remain
visibly separate, and skipped private-data runs print reproduction instructions
instead of invented values. Task-local tests recompute aggregates from bounded
fixtures and validate the schema and JSON-to-Markdown agreement.
