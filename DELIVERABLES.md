# Deliverables map

Every deliverable the assignment asks for, and the file that answers it. Quoted
wording is taken from the assignment text.

The assignment also asks, for all tasks, for "commented code as well as a short text
description on your observations about the problems, the results and why you did the
way you did it". Tasks 1 to 3 carry that account in their README and architecture
documents; tasks 4 to 6 each carry it in `reports/observations.md`.

## Task 1: Internet-search agent

> A fully functional Internet-Search Agent. Documentation of the findings and approach.

Task 1 is answered by two agents. They solve the same problem at opposite ends of one
trade-off, and which one to look at depends on what you want to check.

| | Bounded research agent | Compact baseline agent |
|---|---|---|
| Answers in | verbatim spans copied from the pages it retrieved | its own prose |
| Grounding | every claim machine-verified against stored evidence | the sources the model opened, asserted not verified |
| When no source supports an answer | refuses and says why | answers anyway, with whatever it read |
| Size | a package with a state machine, budgets, and an evidence store | one file, under 400 lines |
| Used by | the Task 2 API and the Task 3 deployment | nothing else; it stands alone |
| Source | [`task-01-search-agent/src/search_agent/`](task-01-search-agent/src/search_agent/) | [`task-01-search-agent/baseline/web_agent.py`](task-01-search-agent/baseline/web_agent.py) |
| Approach and findings | [`task-01-search-agent/README.md`](task-01-search-agent/README.md) | [`task-01-search-agent/baseline/README.md`](task-01-search-agent/baseline/README.md) |

### Trying them

The baseline agent gives a readable answer fastest. It needs Ollama running:

```bash
ollama pull qwen3:8b
make web-agent Q="what does wikipedia say about germany?"
```

It searches, opens a page, and answers in prose with the URLs it opened. A greeting
such as `make web-agent Q="hi"` returns a direct reply with no search, which is the
routing behaviour the task asks for.

The bounded agent runs without Ollama or network access in deterministic mode, which
is the quickest way to see its state machine, its event stream, and its citation
contract:

```bash
uv run --package siemens-search-agent python -m search_agent.cli \
  "Find the latest official Siemens sustainability report." --mode demo
```

For the whole service, including the REST API and the browser page used to drive it:

```bash
make local-acceptance        # deterministic inference, no Ollama needed
make local-live-acceptance   # real local model and live public web search
```

`make local-live-acceptance` offers to install or reuse Ollama, defaults to
`qwen3:8b`, runs the submission checks, then submits a real research request through
the authenticated API and requires a grounded answer with at least one public
citation. Pass `--keep-running` to leave the API and reviewer page up.

### What to expect

The bounded agent abstains often on open-ended live questions. That is the design
rather than a fault: it will not answer from a source it cannot quote exactly, so a
question whose answer exists only as paraphrase, or on a page rendered by JavaScript,
ends in a stated refusal. The baseline agent answers those, and in exchange its
citations are not verified. Both READMEs list their own failure modes together with
the runs that produced them.

### Supporting material

| What | Where |
|---|---|
| Local model selection and benchmarks | [`task-01-search-agent/docs/model-selection.md`](task-01-search-agent/docs/model-selection.md) |
| Routing and grounding evaluation set | [`task-01-search-agent/evals/`](task-01-search-agent/evals/) |
| Diagram | [`docs/presentation/diagrams/task-01-search-agent.svg`](docs/presentation/diagrams/task-01-search-agent.svg) |

## Task 2: API for agent functionality

> Source code for the API. API documentation, including endpoints, request/response
> examples, and usage instructions, if necessary any design principles.

| What | Where |
|---|---|
| API source | [`task-02-agent-api/src/agent_api/`](task-02-agent-api/src/agent_api/) |
| Endpoints, examples, errors, quotas, streaming | [`task-02-agent-api/docs/api.md`](task-02-agent-api/docs/api.md) |
| Operating the service | [`task-02-agent-api/docs/operations.md`](task-02-agent-api/docs/operations.md) |
| Design principles and boundaries | [`task-02-agent-api/README.md`](task-02-agent-api/README.md) |
| Threat model | [`task-02-agent-api/docs/threat-model.md`](task-02-agent-api/docs/threat-model.md) |
| Diagram | [`docs/presentation/diagrams/task-02-agent-api.svg`](docs/presentation/diagrams/task-02-agent-api.svg) |

The status requirement ("what the agent is doing at any time") is served by
`GET /v1/runs/{run_id}` and by the server-sent event stream, both documented in
`api.md`. Run states come from the agent's own state machine rather than a generic
busy flag.

## Task 3: Deployment strategy

> A detailed description outlining the deployment plan, including architecture
> diagrams if applicable.

| What | Where |
|---|---|
| Cloud decision and target architecture | [`task-03-deployment-strategy/architecture/strategy.md`](task-03-deployment-strategy/architecture/strategy.md) |
| Scaling, reliability, and cost at production size | [`task-03-deployment-strategy/architecture/production-scale.md`](task-03-deployment-strategy/architecture/production-scale.md) |
| Capacity model | [`task-03-deployment-strategy/architecture/capacity-model.md`](task-03-deployment-strategy/architecture/capacity-model.md) |
| Incident runbooks | [`task-03-deployment-strategy/architecture/runbooks.md`](task-03-deployment-strategy/architecture/runbooks.md) |
| CI/CD | [`task-03-deployment-strategy/operations/ci-cd.md`](task-03-deployment-strategy/operations/ci-cd.md) |
| Infrastructure as code | [`task-03-deployment-strategy/terraform/`](task-03-deployment-strategy/terraform/) |
| Diagram | [`docs/presentation/diagrams/task-03-deployment-strategy.svg`](docs/presentation/diagrams/task-03-deployment-strategy.svg) |

`strategy.md` carries the provider comparison, the weighted decision, and three
inline diagrams. Scalability, reliability, and security are treated in
`strategy.md` and `production-scale.md`.

## Task 4: Binary classification

> A description of the data preprocessing steps. The code used to develop the binary
> classification model.

| What | Where |
|---|---|
| Preprocessing description | [`task-04-binary-classification/reports/model-card.md`](task-04-binary-classification/reports/model-card.md), section "Data and leakage controls" |
| Observations, results, and reasoning | [`task-04-binary-classification/reports/observations.md`](task-04-binary-classification/reports/observations.md) |
| Model development code | [`task-04-binary-classification/src/binary_classification/`](task-04-binary-classification/src/binary_classification/) |
| Reproducible data profile | [`task-04-binary-classification/reports/data-analysis.md`](task-04-binary-classification/reports/data-analysis.md) |
| Machine-readable metrics | [`task-04-binary-classification/reports/metrics.json`](task-04-binary-classification/reports/metrics.json) |
| Diagram | [`docs/presentation/diagrams/task-04-binary-classification.svg`](docs/presentation/diagrams/task-04-binary-classification.svg) |

Start with `observations.md`: the duplicate rows, the imbalance, and the repeated
feature vectors explain why the join and the split take more care than the model.

## Task 5: Identifying alternative materials

> A descriptive analysis of the data, including identified issues and potential
> solutions. The code used to develop the similarity identification model based on
> "Part Description".

| Assignment step | Where |
|---|---|
| Step 1, descriptive analysis, difficulties, solutions | [`task-05-material-similarity/reports/data-analysis.md`](task-05-material-similarity/reports/data-analysis.md) |
| Step 2, the model and the explanation of the approach | [`task-05-material-similarity/reports/observations.md`](task-05-material-similarity/reports/observations.md), with metrics in [`retrieval-evaluation.md`](task-05-material-similarity/reports/retrieval-evaluation.md) |
| Step 3, extending to all other attributes | [`task-05-material-similarity/reports/hybrid-extension-design.md`](task-05-material-similarity/reports/hybrid-extension-design.md) |
| Similarity model code | [`task-05-material-similarity/src/material_similarity/`](task-05-material-similarity/src/material_similarity/) |
| Diagram | [`docs/presentation/diagrams/task-05-material-similarity.svg`](docs/presentation/diagrams/task-05-material-similarity.svg) |

## Task 6: Functions for categorical attributes

> Source code of the function. Explain the utility of this function in constructing a
> logistic regression model. Briefly describe an alternative method. Additionally,
> provide a short overview of an algorithm that inherently addresses this issue
> without requiring pre-treatment.

| Assignment item | Where |
|---|---|
| Source code of the function | [`task-06-category-consolidation/src/category_consolidation/core.py`](task-06-category-consolidation/src/category_consolidation/core.py) |
| Utility for logistic regression | [`task-06-category-consolidation/reports/observations.md`](task-06-category-consolidation/reports/observations.md), section "Why this helps a logistic regression predicting a binary outcome" |
| Alternative method | Same file, section "An alternative for a high-cardinality attribute" |
| Algorithm needing no pre-treatment | Same file, section "An algorithm that needs no pre-treatment" |
| Usage and options | [`task-06-category-consolidation/README.md`](task-06-category-consolidation/README.md) |
| Diagram | [`docs/presentation/diagrams/task-06-category-consolidation.svg`](docs/presentation/diagrams/task-06-category-consolidation.svg) |

## Reading the repository

| Purpose | Where |
|---|---|
| Fastest route in for a reviewer | [`docs/reviewer-guide.md`](docs/reviewer-guide.md) |
| Cross-task architecture | [`docs/architecture.md`](docs/architecture.md) |
| Setup, expected output, platform notes | [`docs/getting-started.md`](docs/getting-started.md) |
| Common failures | [`docs/troubleshooting.md`](docs/troubleshooting.md) |

## Running it

`make local-submission` builds a temporary environment, installs all six packages,
and runs formatting, linting, typing, the full test suite, and the submission audit.
It needs no cloud account and no external LLM.

Tasks 4 and 5 use source tables that are excluded from Git. Without them the suite
reports three explicit skips. To include those checks:

```bash
export SIEMENS_TASK4_INPUT_DIR=/absolute/path/to/task4-input
export SIEMENS_FUSE_CSV=/absolute/path/to/Fuse.csv
make local-submission
```
