# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG PYTHON_IMAGE=python:3.12.14-alpine3.23@sha256:31a768b01976652c222e318fe5bd6e7c252f056cbf489c88fa256f1bf0af58e3
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.6@sha256:88bc6eb1ccd4b82efd0e1b530caffabddf50dc2bf612e66c14ea25b8ee8a4d3d

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY task-01-search-agent/pyproject.toml task-01-search-agent/pyproject.toml
COPY task-01-search-agent/src task-01-search-agent/src
COPY task-02-agent-api/pyproject.toml task-02-agent-api/pyproject.toml
COPY task-02-agent-api/src task-02-agent-api/src
COPY task-03-deployment-strategy/pyproject.toml task-03-deployment-strategy/pyproject.toml
COPY task-03-deployment-strategy/src task-03-deployment-strategy/src
RUN --mount=type=cache,target=/tmp/uv-cache \
    uv sync --frozen --no-dev --no-editable --package siemens-deployment-strategy

FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="Siemens research agent assessment API" \
      org.opencontainers.image.description="Bounded non-root FastAPI research-agent service" \
      org.opencontainers.image.version="0.1.0"

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    PORT=8080 \
    AGENT_API_DATABASE_PATH=/tmp/agent-api.sqlite3 \
    AGENT_API_INFERENCE_MODE=fake \
    AGENT_API_SHUTDOWN_SECONDS=10

# The pinned upstream image predates these Alpine security revisions. Exact package
# versions make the patch layer fail closed instead of drifting during rebuilds.
RUN apk add --no-cache --upgrade \
    libcrypto3=3.5.8-r0 \
    libssl3=3.5.8-r0 \
    sqlite-libs=3.53.4-r0

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER 65532:65532
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-I", "-B", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health/ready', timeout=2).read()"]

ENTRYPOINT ["python", "-I", "-u", "-B", "-m", "deployment_strategy.container"]
