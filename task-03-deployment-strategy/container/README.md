# Hardened assessment container

The image packages the locked Task 1 agent, Task 2 API, and the smallest Task 3
process adapter. The default `fake` inference mode is deterministic and offline so
build and smoke success never depends on a model or cloud credential.

Build from the repository root. The base image is an OCI-index digest with both the
local Linux arm64 and Cloud Run Linux amd64 variants:

```bash
docker buildx build --platform linux/arm64 --load \
  -f task-03-deployment-strategy/container/Dockerfile \
  -t siemens-agent-api:c02 .

docker buildx build --platform linux/amd64 \
  -f task-03-deployment-strategy/container/Dockerfile .
```

Run the local smoke with a read-only root, an explicit writable tmpfs, no Linux
capabilities, and the API-key pepper supplied through the environment rather than a
build layer or command-line value:

```bash
export AGENT_API_KEY_PEPPER="$(python -c \
  'import base64; print(base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("="))')"
docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL --security-opt no-new-privileges \
  --env AGENT_API_KEY_PEPPER -p 8080:8080 siemens-agent-api:c02
```

`GET /health/live` proves the process is responsive. `GET /health/ready` also checks
the SQLite schema and quota store. Uvicorn owns SIGTERM and drains the local worker
within `AGENT_API_SHUTDOWN_SECONDS` (1 to 30 seconds).

Generate and scan the final image before promotion:

```bash
syft siemens-agent-api:c02 \
  -o cyclonedx-json=/tmp/siemens-agent-api-sbom.cdx.json
trivy image --exit-code 1 --severity HIGH,CRITICAL --scanners vuln,secret \
  siemens-agent-api:c02
gitleaks git --redact --no-banner --exit-code 1 .
```

Set `AGENT_API_INFERENCE_MODE=ollama`, `AGENT_MODEL_NAME`, and optionally
`AGENT_MODEL_BASE_URL` to run the same bounded agent against a local Ollama server.
In this local mode the process opens the existing SQLite semantic/procedural memory
repositories per request and automatically supplies their reviewed active context to
the agent. The terminal API response records whether any memory was actually used.

Cloud workers use the same `OllamaResearchExecutor` and inject a fresh Google ID
token for each request to a private HTTPS model origin. The agent consumes memory
through `ReviewedMemoryReadPort`; a scaled deployment supplies a managed-store
adapter instead of sharing SQLite between replicas. The assessment cloud profile
does not pretend SQLite is authoritative and therefore leaves that adapter unset.
`disabled` starts the API without an in-process worker, and unknown inference modes
fail closed during startup.
