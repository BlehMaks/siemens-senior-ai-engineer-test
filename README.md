# Siemens Senior AI Engineer technical assignment

This repository contains six independent deliverables from the supplied technical assignment. I treat Tasks 1 to 3 as one deployable system with explicit boundaries between the agent, its API, and cloud infrastructure. Tasks 4 to 6 remain small, reproducible data-science packages.

The implementation order follows the assignment's stated priority:

1. [Internet-search agent](task-01-search-agent/README.md)
2. [API for agent functionality](task-02-agent-api/README.md)
3. [Deployment strategy](task-03-deployment-strategy/README.md)
4. [Binary classification](task-04-binary-classification/README.md)
5. [Alternative-material retrieval](task-05-material-similarity/README.md)
6. [Categorical-value consolidation](task-06-category-consolidation/README.md)

## Repository design

Each task owns its code, tests, decision notes, and usage instructions. Tasks 1 to 3 will share typed contracts rather than copy agent logic across folders. A root Python workspace can link those packages while keeping each deliverable independently understandable.

The repository uses Python unless a task explicitly requires another language. The baseline is Python 3.12 with `uv` for reproducible environments, `pytest` for executable acceptance checks, `ruff` for linting and formatting, and static type checking on production code.

## Requirement boundary

Each task README separates two scopes:

- Assignment baseline: requirements and deliverables stated in the supplied document.
- Engineering extension: additional work intended to demonstrate production judgment. These items do not replace or reinterpret the assignment.

The source document contains an inconsistent reference to "all four tasks" even though it defines six. This repository follows the six task sections and preserves the repeated instruction to prioritize the first three.

## Quality bar

All runnable work must include setup and usage instructions, documented assumptions, deterministic checks, and a short explanation of results. Comments explain decisions, invariants, or security boundaries; they do not narrate obvious syntax. The common review and documentation rules are recorded in [docs/engineering-standards.md](docs/engineering-standards.md).

Raw assignment files and private working material are kept outside the submission history until redistribution rights and the final delivery format are confirmed. The intended publication boundary is documented in [docs/submission-boundary.md](docs/submission-boundary.md).
