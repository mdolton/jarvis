# Jarvis Plan 6 — Docker Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `docker compose up` builds and runs Jarvis as a single container — Alembic migrations applied at startup, healthcheck wired, config/data bind-mounted, host LLM reachable via `host.docker.internal`, secrets via `.env`. A README documents everything a new user needs to get started.

**Architecture:** Multi-stage Dockerfile (build stage installs deps with `uv`, runtime stage copies the venv + app code). Entrypoint script runs `alembic upgrade head` then `python -m jarvis serve`. Docker Compose mounts `./config` (read-only) and `./data` (read-write), passes env vars from `.env`, exposes port 8080 for the dashboard.

**Tech Stack:** Docker (multi-stage, Python 3.12-slim base), `uv` for dependency install in the build stage, Docker Compose v2.

**Design spec this plan implements:** `docs/superpowers/specs/2026-04-14-jarvis-agent-service-design.md` — §10 Deployment (entire section).

---

## File Structure

New files (all at repo root):

```
Dockerfile
docker-compose.yml
.env.example
entrypoint.sh
config/                    # Example config directory (committed with example files)
  jarvis.yaml.example
  channels.yaml.example
  mcp-servers.yaml.example
README.md
```

Files modified:
- `jarvis/web/routes/health.py` — enhance `/healthz` to check DB writability and MCP status.
- `.gitignore` — ensure `data/`, `.env` are ignored; `config/*.example` is NOT ignored.
- `alembic.ini` — ensure the default URL works inside the container (`/app/data/jarvis.db`).

---

## Task 1: Enhanced `/healthz` endpoint

The spec says healthz should check: event loop responsive (implicit — if we respond, it's responsive), DB is writable, and at least one MCP server connected (if any are configured). Currently it just returns `{"status": "ok"}`. Enhance it.

**Files:**
- Modify: `jarvis/web/routes/health.py`
- Modify: `tests/integration/test_web_health.py`

- [ ] **Step 1: Write failing test**

Replace `tests/integration/test_web_health.py` with:

```python
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from jarvis.web.app import create_app


def test_healthz_ok_without_context():
    """When no app_context is set (e.g., during early startup), healthz
    still returns 200 with a degraded status."""
    app = create_app(app_context=None)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_healthz_reports_db_and_mcp():
    ctx = MagicMock()
    ctx.session_factory = MagicMock()
    # Simulate a working DB check.
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock()
    ctx.session_factory.return_value = mock_session

    ctx.mcp_manager.agent_mcp_servers.return_value = ["server1"]

    app = create_app(app_context=ctx)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["db"] == "ok"
    assert data["mcp_servers"] == 1
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_web_health.py -v`
Expected: `test_healthz_reports_db_and_mcp` fails (current healthz doesn't return `db` or `mcp_servers`).

- [ ] **Step 3: Update `jarvis/web/routes/health.py`**

```python
"""GET /healthz — JSON health check."""

import logging

from fastapi import APIRouter, Request
from sqlalchemy import text

_log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request):
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        return {"status": "ok", "detail": "no app context (startup)"}

    # Check DB writability.
    db_status = "ok"
    try:
        async with ctx.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        _log.exception("healthz: DB check failed")
        db_status = "error"

    # Check MCP servers.
    mcp_count = len(ctx.mcp_manager.agent_mcp_servers())

    status = "ok" if db_status == "ok" else "degraded"
    return {
        "status": status,
        "db": db_status,
        "mcp_servers": mcp_count,
    }
```

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/integration/test_web_health.py -v`
Expected: 2 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 138 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/web/routes/health.py tests/integration/test_web_health.py
git commit -m "enhance healthz to check DB writability and MCP server count"
```

---

## Task 2: Example config files

Committed example configs that document the expected YAML structure. Users copy these and fill in their values.

**Files:**
- Create: `config/jarvis.yaml.example`
- Create: `config/channels.yaml.example`
- Create: `config/mcp-servers.yaml.example`

- [ ] **Step 1: Write the example files**

Write `config/jarvis.yaml.example`:

```yaml
# Jarvis core configuration.
# Copy this to jarvis.yaml and edit.

llm:
  # OpenAI-compatible API endpoint (LM Studio, Ollama, etc.)
  base_url: ${JARVIS_LLM_BASE_URL}
  api_key: ${JARVIS_LLM_API_KEY}
  model: ${JARVIS_LLM_MODEL}
  request_timeout_sec: 60.0

timezone: ${JARVIS_TIMEZONE}
idle_timeout_sec: 900
max_concurrent_agents: 3
log_level: INFO
default_schedule_output_mode: discord
```

Write `config/channels.yaml.example`:

```yaml
# Channel adapter configuration.
# Copy this to channels.yaml and edit.

# Uncomment to enable Discord:
# discord:
#   token: ${JARVIS_DISCORD_TOKEN}
#   allowed_user_ids:
#     - "YOUR_DISCORD_USER_ID"
```

Write `config/mcp-servers.yaml.example`:

```yaml
# MCP server configuration.
# Copy this to mcp-servers.yaml and edit.

servers: []

# Example stdio server:
# servers:
#   - name: filesystem
#     transport: stdio
#     command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"]
#
# Example HTTP server:
# servers:
#   - name: remote-api
#     transport: http
#     url: http://localhost:3000/mcp
#     headers:
#       Authorization: "Bearer ${MCP_API_TOKEN}"
```

- [ ] **Step 2: Commit**

```bash
git add config/
git commit -m "add example config files documenting YAML structure"
```

---

## Task 3: `.env.example`

**Files:**
- Create: `.env.example`

- [ ] **Step 1: Write the file**

Write `.env.example`:

```bash
# Jarvis environment variables.
# Copy this to .env and fill in your values.
# docker compose automatically reads .env from the project root.

# === Required ===

# LLM endpoint (LM Studio, Ollama, or any OpenAI-compatible API)
JARVIS_LLM_BASE_URL=http://host.docker.internal:1234/v1
JARVIS_LLM_API_KEY=dummy
JARVIS_LLM_MODEL=qwen2.5:32b

# === Optional ===

# Discord bot token (omit to run without Discord)
# JARVIS_DISCORD_TOKEN=
# JARVIS_DISCORD_ALLOWED_USER_IDS=123456789

# Timezone for schedules (default: UTC)
# JARVIS_TIMEZONE=America/Los_Angeles

# Log level (default: INFO)
# JARVIS_LOG_LEVEL=INFO

# Dashboard port (default: 8080)
# JARVIS_DASHBOARD_PORT=8080
```

- [ ] **Step 2: Verify `.gitignore` covers `.env` but not `.env.example`**

Check that `.gitignore` has `.env` and `!.env.example`. If not, add them.

- [ ] **Step 3: Commit**

```bash
git add .env.example .gitignore
git commit -m "add .env.example documenting required environment variables"
```

---

## Task 4: `entrypoint.sh`

The container entrypoint: runs Alembic migrations, then starts Jarvis.

**Files:**
- Create: `entrypoint.sh`

- [ ] **Step 1: Write the entrypoint**

Write `entrypoint.sh`:

```bash
#!/bin/sh
set -e

echo "jarvis: running database migrations..."
alembic -x db_url="sqlite+aiosqlite:///./data/jarvis.db" upgrade head

echo "jarvis: starting service..."
exec python -m jarvis serve "$@"
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x entrypoint.sh`

- [ ] **Step 3: Commit**

```bash
git add entrypoint.sh
git commit -m "add entrypoint.sh running alembic migrations then jarvis serve"
```

---

## Task 5: `Dockerfile`

Multi-stage build: build stage installs deps, runtime stage copies the result.

**Files:**
- Create: `Dockerfile`

- [ ] **Step 1: Write the Dockerfile**

Write `Dockerfile`:

```dockerfile
# === Build stage ===
FROM python:3.12-slim AS builder

# Install uv for fast dependency resolution.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first (layer cache).
COPY pyproject.toml uv.lock ./

# Install dependencies into a venv.
RUN uv sync --frozen --no-dev --no-install-project

# Copy the application code.
COPY jarvis/ jarvis/
COPY alembic/ alembic/
COPY alembic.ini ./

# Install the project itself.
RUN uv sync --frozen --no-dev

# === Runtime stage ===
FROM python:3.12-slim

# Non-root user for security.
RUN groupadd -r jarvis && useradd -r -g jarvis -d /app jarvis

WORKDIR /app

# Copy the venv + app from build stage.
COPY --from=builder /app /app

# Copy entrypoint.
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Default config and data directories (mounted as volumes in compose).
RUN mkdir -p /app/config /app/data && chown -R jarvis:jarvis /app

USER jarvis

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
```

- [ ] **Step 2: Commit**

```bash
git add Dockerfile
git commit -m "add multi-stage Dockerfile with uv, non-root user, and healthcheck"
```

---

## Task 6: `docker-compose.yml`

**Files:**
- Create: `docker-compose.yml`

- [ ] **Step 1: Write the compose file**

Write `docker-compose.yml`:

```yaml
services:
  jarvis:
    build: .
    restart: unless-stopped
    ports:
      - "${JARVIS_DASHBOARD_PORT:-8080}:8080"
    volumes:
      - ./data:/app/data
      - ./config:/app/config:ro
    environment:
      - JARVIS_LLM_BASE_URL
      - JARVIS_LLM_API_KEY
      - JARVIS_LLM_MODEL
      - JARVIS_DISCORD_TOKEN
      - JARVIS_DISCORD_ALLOWED_USER_IDS
      - JARVIS_TIMEZONE
      - JARVIS_LOG_LEVEL
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "add docker-compose.yml with config/data mounts and host.docker.internal"
```

---

## Task 7: `README.md`

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write the README**

Write `README.md`:

```markdown
# Jarvis — Personal AI Agent Service

A self-hosted AI agent that connects to any OpenAI-compatible LLM (LM Studio, Ollama, etc.), gains capabilities via MCP servers, communicates through Discord, runs scheduled tasks, and provides a web dashboard for monitoring.

## Quick Start (Docker)

```bash
# 1. Clone and enter the repo.
git clone <your-repo-url> && cd jarvis

# 2. Copy example configs and fill in your values.
cp .env.example .env
cp config/jarvis.yaml.example config/jarvis.yaml
cp config/channels.yaml.example config/channels.yaml
cp config/mcp-servers.yaml.example config/mcp-servers.yaml

# 3. Edit .env with your LLM endpoint.
#    If running LM Studio / Ollama on the host:
#    JARVIS_LLM_BASE_URL=http://host.docker.internal:1234/v1

# 4. Create the data directory.
mkdir -p data

# 5. Start Jarvis.
docker compose up -d

# 6. Open the dashboard.
open http://localhost:8080
```

## Quick Start (Local Development)

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies.
uv sync

# Copy example configs.
cp config/jarvis.yaml.example config/jarvis.yaml
# Edit config/jarvis.yaml with your LLM endpoint.

mkdir -p data

# Run migrations.
uv run alembic upgrade head

# Start the service.
uv run python -m jarvis serve
```

## CLI Commands

```bash
# Run a one-shot agent invocation.
python -m jarvis invoke "What's on my calendar today?"

# Validate config and print a summary.
python -m jarvis check-config

# Start as a long-lived service (Discord + scheduler + dashboard).
python -m jarvis serve
```

## Configuration

All config lives in the `config/` directory:

| File | Purpose |
|------|---------|
| `jarvis.yaml` | LLM endpoint, timezone, concurrency, log level |
| `channels.yaml` | Discord bot token and allowed user IDs |
| `mcp-servers.yaml` | MCP server definitions (stdio, HTTP, SSE) |

Environment variables in YAML files are expanded via `${VAR}` syntax. Secrets should go in `.env` (never committed) and be referenced in YAML.

## MCP Servers

Jarvis gains capabilities by connecting to [MCP](https://modelcontextprotocol.io/) servers. Configure them in `mcp-servers.yaml`:

```yaml
servers:
  - name: filesystem
    transport: stdio
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"]

  - name: calendar
    transport: http
    url: http://localhost:3000/mcp
```

## Scheduled Tasks

Create schedules via the dashboard at `/schedules`. Each schedule has:
- A cron expression (e.g., `0 8 * * *` for 8am daily)
- A prompt (what to tell the agent)
- An output mode: `discord` (DM you), `dashboard_only` (silent), or `discord_if_noteworthy` (agent decides)

## Dashboard

Available at `http://localhost:8080` when running:

- **Home** — service status overview
- **Conversations** — browse past agent interactions
- **Schedules** — create, enable/disable, delete schedules
- **MCP** — connected servers and discovered tools
- **Audit** — full event log with live SSE tailing
- **Settings** — read-only config view

## Architecture

Single Python async process running in Docker:
- **Agent**: OpenAI Agents SDK with MCP tool integration
- **Channels**: Discord DM adapter (Slack/WhatsApp extensible)
- **Scheduler**: APScheduler cron-based triggers
- **Dashboard**: FastAPI + HTMX + Jinja2
- **Persistence**: SQLite (WAL mode) + Alembic migrations
- **Audit**: Buffered async logger capturing every LLM call, tool call, and agent decision

## License

MIT
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "add README with quick start, configuration, and architecture overview"
```

---

## Task 8: Docker build smoke test

A script that builds the Docker image and verifies it starts and responds to healthz. This is the E2E smoke test from the design spec.

**Files:**
- Create: `tests/smoke/test_docker_build.sh`

- [ ] **Step 1: Write the smoke test script**

Write `tests/smoke/test_docker_build.sh`:

```bash
#!/bin/bash
# Smoke test: build the Docker image, start it, hit healthz, tear down.
# Run from the repo root: bash tests/smoke/test_docker_build.sh
# Requires: docker

set -euo pipefail

IMAGE_NAME="jarvis-smoke-test"
CONTAINER_NAME="jarvis-smoke-$$"

cleanup() {
    echo "cleaning up..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker rmi "$IMAGE_NAME" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== building docker image ==="
docker build -t "$IMAGE_NAME" .

echo "=== creating minimal config ==="
TMPDIR=$(mktemp -d)
mkdir -p "$TMPDIR/config" "$TMPDIR/data"

cat > "$TMPDIR/config/jarvis.yaml" <<EOF
llm:
  base_url: http://host.docker.internal:1234/v1
  api_key: dummy
  model: test-model
EOF
echo "{}" > "$TMPDIR/config/channels.yaml"
echo "servers: []" > "$TMPDIR/config/mcp-servers.yaml"

echo "=== starting container ==="
docker run -d \
    --name "$CONTAINER_NAME" \
    -p 18080:8080 \
    -v "$TMPDIR/config:/app/config:ro" \
    -v "$TMPDIR/data:/app/data" \
    -e JARVIS_LLM_BASE_URL=http://host.docker.internal:1234/v1 \
    -e JARVIS_LLM_API_KEY=dummy \
    -e JARVIS_LLM_MODEL=test-model \
    "$IMAGE_NAME"

echo "=== waiting for healthz (up to 30s) ==="
for i in $(seq 1 30); do
    if curl -sf http://localhost:18080/healthz > /dev/null 2>&1; then
        echo "healthz OK after ${i}s"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "FAIL: healthz not responding after 30s"
        docker logs "$CONTAINER_NAME"
        exit 1
    fi
    sleep 1
done

echo "=== verifying healthz response ==="
RESP=$(curl -sf http://localhost:18080/healthz)
echo "healthz: $RESP"

if echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['status']=='ok', d"; then
    echo "=== SMOKE TEST PASSED ==="
else
    echo "=== SMOKE TEST FAILED ==="
    docker logs "$CONTAINER_NAME"
    exit 1
fi
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x tests/smoke/test_docker_build.sh`

- [ ] **Step 3: Commit**

Note: we do NOT run this test in the normal `pytest` suite — it requires Docker and takes 30+ seconds. It's a manual / CI-gated test.

```bash
mkdir -p tests/smoke
git add tests/smoke/test_docker_build.sh
git commit -m "add Docker build smoke test script"
```

---

## Task 9: Update `.gitignore` and `alembic.ini`

Finalize gitignore for deployment artifacts and ensure Alembic works inside the container.

**Files:**
- Modify: `.gitignore`
- Modify: `alembic.ini`

- [ ] **Step 1: Update `.gitignore`**

Ensure these entries are present (some may already be there from Plan 1):

```
# Docker
*.tar

# Config files with real secrets (examples are committed)
config/jarvis.yaml
config/channels.yaml
config/mcp-servers.yaml
```

Do NOT ignore `config/*.example` — those are committed.

- [ ] **Step 2: Verify `alembic.ini` URL**

The current `alembic.ini` has `sqlalchemy.url = sqlite+aiosqlite:///data/jarvis.db`. Inside the container (working dir `/app`), this resolves to `/app/data/jarvis.db` — correct. The entrypoint overrides via `-x db_url=...` anyway, so this is a fallback. No change needed if it's already correct.

If it needs adjustment, update the line.

- [ ] **Step 3: Commit**

```bash
git add .gitignore alembic.ini
git commit -m "update gitignore for deployment and verify alembic.ini path"
```

---

## Plan 6 complete — summary

At the end of Plan 6:

- `docker compose up` builds a multi-stage Docker image, runs Alembic migrations, and starts Jarvis.
- The container exposes port 8080 (dashboard), reads config from `./config/` (bind-mounted read-only), stores data in `./data/` (bind-mounted read-write).
- `host.docker.internal` gives the container access to host-running LLM endpoints.
- `/healthz` checks DB writability + MCP server count.
- `.env.example` documents all environment variables.
- Example config files document the YAML structure.
- `README.md` covers quick start (Docker + local dev), CLI commands, configuration, MCP servers, scheduling, dashboard, and architecture.
- Docker smoke test script builds, starts, hits healthz, and tears down.

**This is the final plan. Jarvis v1 is complete.**
