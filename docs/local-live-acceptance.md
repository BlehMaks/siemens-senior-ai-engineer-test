# Local live-model acceptance

This workflow verifies the complete local agent path with a real Ollama model,
public web search, page fetching, grounded synthesis, and the durable Tasks 1–3
API. It never substitutes the fake executor for the live phase.

## Fast interactive path

From the repository root run:

```bash
make local-live-acceptance
```

The script first asks whether it should:

1. install/start Ollama and download the selected model if necessary; or
2. reuse an existing Ollama installation and model, configuring only the agent
   and API wrapper.

It then asks for an Ollama model tag. Press Enter to use `qwen3:8b`, the smallest
Qwen candidate in the frozen matrix (about 5.2 GB). This default is a convenient
live-smoke choice, not a completed production model selection.

The workflow performs these gates:

1. starts or reuses the loopback Ollama service;
2. pulls or validates the requested model;
3. runs `make local-submission` unless explicitly skipped;
4. starts the API with `AGENT_API_INFERENCE_MODE=ollama`, keyless Brave search,
   and bounded DDGS metasearch as the ordered fallback;
5. submits a real research request through the authenticated REST API;
6. waits for completion and requires a non-empty grounded answer with at least one
   public-web citation;
7. verifies through Ollama `/api/ps` that the requested model was actually loaded;
8. optionally leaves the API running for manual reviewer use.

Generated results, server logs, model metadata, and the SQLite run database are
written under `artifacts/local/live-acceptance/<UTC timestamp>/`. That directory
is ignored by Git.

## Include the private Tasks 4 and 5 checks

Export the private input paths before starting the workflow:

```bash
export SIEMENS_TASK4_INPUT_DIR="/absolute/path/to/task4"
export SIEMENS_FUSE_CSV="/absolute/path/to/Fuse.csv"
make local-live-acceptance
```

Without these variables the deterministic public suite still runs, but its three
owner-private checks remain explicit skips.

## Non-interactive reviewer commands

Install or prepare Ollama, pull `qwen3:8b`, run all deterministic checks, and then
run the live API acceptance:

```bash
./task-03-deployment-strategy/scripts/local_live_acceptance.sh \
  --setup install \
  --model qwen3:8b
```

Reuse a model that is already installed:

```bash
./task-03-deployment-strategy/scripts/local_live_acceptance.sh \
  --setup existing \
  --model qwen3:8b
```

If the deterministic repository gate already passed at the same commit, omit the
repeat while retaining the real-model and public-web gates:

```bash
./task-03-deployment-strategy/scripts/local_live_acceptance.sh \
  --setup existing \
  --model qwen3:8b \
  --skip-deterministic
```

Use `--keep-running` to leave the API and reviewer UI available until Ctrl+C. Use
`--query "..."` to replace the default Siemens sustainability research request.
The query must remain between 3 and 400 characters.

## Requirements and boundaries

- macOS, Linux, or WSL2 with Git, Make, `uv`, `curl`, `jq`, and OpenSSL;
- enough free disk and memory for the selected model;
- outbound access to Ollama's model registry, DDGS search providers, and public
  source pages;
- a free loopback port, default `8093` for the API and `11434` for Ollama.

Automatic macOS installation uses Homebrew. Automatic Linux/WSL2 installation
downloads the official `https://ollama.com/install.sh` into a temporary file and
executes it only after the user selects the install path. The script never pipes a
remote installer directly into a shell.

This workflow validates one selected model end to end. It does not replace the
separate frozen four-candidate performance and safety benchmark described in
[`task-01-search-agent/docs/model-selection.md`](../task-01-search-agent/docs/model-selection.md).
