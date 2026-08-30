# ADR 0007: Keep business extensions outside assignment baselines

## Context

Tasks 4–6 already satisfy independent assignment contracts. Business-oriented
decision, safety, and integration behavior is useful, but it must not silently
change the reviewed defaults or couple otherwise independent packages.

## Decision

Keep assignment commands and schemas as the default. Add extensions through
explicit modes or separately named APIs and compare them in task-local versioned
reports. Tasks share root development tooling and the documented report envelope,
not runtime code. See the editable
[context diagram](../diagrams/business-extension-context.mmd).

## Rejected alternatives

- A common extension framework creates coupling without shared runtime behavior.
- Replacing baseline defaults makes historical evidence and reviewer expectations
  ambiguous.
- A service, database, or dashboard adds no value at the present local data scale.

## Consequences

Each package owns its dependencies, report generator, compatibility tests, and
versioning. Some small report code is intentionally repeated. Baseline evidence
remains independently reproducible and private data remains outside Git.
