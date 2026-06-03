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

## Deploying to Production

Production runs a pre-built image from GitHub Container Registry (ghcr.io). You
build and push from a dev machine; the server pulls and runs it. The image is
multi-arch (`linux/amd64` + `linux/arm64`), so it runs on x86 or ARM hosts.
Database migrations run automatically on container start (see `entrypoint.sh`).

### 1. Build and push the image (dev machine)

```bash
make check                       # lint + tests
export CR_PAT=ghp_xxx            # GitHub PAT with the write:packages scope
make login                       # docker login ghcr.io
make deploy                      # build multi-arch + push :<git-sha> and :latest
```

`make deploy` pushes `ghcr.io/mdolton/jarvis:<git-sha>` and `:latest`. The
package is **private** by default — either keep it private and `docker login`
on the server, or make it public in the GitHub package settings after the first
push.

### 2. Run it on the server

The server needs Docker, `docker-compose.prod.yml`, the `config/` directory,
and a `.env`. Clone the repo (or copy those files), then:

```bash
# Configure.
cp .env.example .env             # set JARVIS_BASE_URL=https://jarvis.moltonlava.online,
                                 # JARVIS_SECRETS_KEY, GOOGLE_OAUTH_* etc.
cp config/jarvis.yaml.example config/jarvis.yaml   # + channels/mcp-servers as needed
mkdir -p data

# If the ghcr package is private, authenticate to pull:
export CR_PAT=ghp_xxx
make login

# Pull and start.
make prod-pull
make prod-up
```

On Linux, the bind-mounted `./data` must be writable by the container's user, or
SQLite fails with `unable to open database file`. The container runs as
`JARVIS_UID:JARVIS_GID` (set in `.env`, default `1000:1000`) — set these to the
owner of `./data` (`id -u` / `id -g`). If `./data` is already root-owned, fix it
with `sudo chown -R "$(id -u):$(id -g)" data`. (Docker Desktop on macOS ignores
ownership, so this only bites on a real Linux host.)

The dashboard listens on port 8080. For `https://jarvis.moltonlava.online`
(required for the Google OAuth redirect URI to work), terminate TLS at your
reverse proxy and forward to the container's port 8080.

### 3. Ship an update

```bash
# Dev machine:
make deploy

# Server:
make prod-pull && make prod-up
```

Pin a specific build instead of `latest` by exporting `JARVIS_IMAGE_TAG` (e.g.
the git short SHA from `make deploy`) before `make prod-pull`/`make prod-up`.

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

### Model selection

Jarvis discovers available models from your LLM endpoint's `/v1/models`.

- **Interactive model** (Discord DMs + manual runs): change it from the
  dashboard **Settings** page (model dropdown) or with the Discord
  `/model` command:
  - `/model current` — show the active model
  - `/model list` — list available models
  - `/model set <name>` — set it (autocompletes; choose **default** to use the
    `llm.model` from `jarvis.yaml`)
  The selection is stored in the database and survives restarts.

- **Per-schedule model**: when creating a schedule on the dashboard, pick a
  model (or **Use default model**). Scheduled runs are independent of the
  interactive selection. If a schedule's pinned model is no longer available,
  the run automatically falls back to the `jarvis.yaml` model (recorded in the
  audit log); interactive runs instead reply with an error so you can re-pick.

> **Discord DMs:** for `/model` to appear in DMs, install the application as a
> user-installable app with the DM context enabled in the Discord Developer
> Portal (Installation → User Install). Guild-only installs expose `/model`
> in servers only.

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

## OAuth-protected MCP servers

Jarvis connects to OAuth-protected HTTP MCP servers from the `/mcp` dashboard. Three
providers ship in the catalog:

- **Fastmail** — Dynamic Client Registration (DCR); no manual client setup.
- **Gmail** — Google's official Gmail MCP server (`https://gmailmcp.googleapis.com/mcp/v1`).
  Google does not support DCR, so you create the OAuth client by hand and supply its
  credentials via environment variables.
- **Google Calendar** — Google's official Calendar MCP server
  (`https://calendarmcp.googleapis.com/mcp/v1`). Manual-mode like Gmail, and reuses the
  same Google OAuth client credentials.

### One-time setup (all providers)

1. Generate a Fernet secrets key:
   ```bash
   uv run python -c "from jarvis.oauth.crypto import generate_key; print(generate_key())"
   ```
2. Set the base env vars (in `.env` for Docker, or your shell for local dev):
   ```
   JARVIS_SECRETS_KEY=<paste-the-key>
   JARVIS_BASE_URL=https://jarvis.moltonlava.online   # or http://localhost:8080 locally
   ```

### Gmail-specific setup

In Google Cloud Console:

1. Enable the **Gmail API** and request access to the **Gmail MCP server** (early access).
2. Configure the **OAuth consent screen** with scopes
   `https://www.googleapis.com/auth/gmail.readonly` and
   `https://www.googleapis.com/auth/gmail.compose`. While the app is unverified, add your
   Google account as a **test user**.
3. Create an **OAuth client ID** of type **Web application**:
   - **Authorized redirect URI:** `https://jarvis.moltonlava.online/oauth/callback`
     (must equal `${JARVIS_BASE_URL}/oauth/callback` exactly).
   - **Authorized JavaScript origins:** leave empty — Jarvis uses a server-side code flow.

Then add the client credentials to Jarvis's environment:
```
GOOGLE_OAUTH_CLIENT_ID=<from Google Cloud Console>
GOOGLE_OAUTH_CLIENT_SECRET=<from Google Cloud Console>
```

Restart Jarvis, open `/mcp`, and click **Connect** on the Gmail card. Jarvis stores the
encrypted tokens and refreshes them automatically; if a refresh permanently fails the card
shows **Needs re-auth** with a Reconnect button.

### Google Calendar-specific setup

Calendar reuses the same Google Cloud OAuth client as Gmail (one Web-application client can
request any scopes), so there are **no extra environment variables** — just expand the
existing client's project. In Google Cloud Console:

1. Enable the **Google Calendar API** (`calendar-json.googleapis.com`) and the **Google
   Calendar MCP API** (`calendarmcp.googleapis.com`). The MCP API is part of the Google
   Workspace Developer Preview Program (early access) — enroll your project there first.
2. Add the Calendar scopes to the **OAuth consent screen**:
   `https://www.googleapis.com/auth/calendar.calendarlist.readonly`,
   `https://www.googleapis.com/auth/calendar.events.freebusy`, and
   `https://www.googleapis.com/auth/calendar.events.readonly`. The existing Web-application
   client and its redirect URI (`${JARVIS_BASE_URL}/oauth/callback`) are reused unchanged.

Restart Jarvis, open `/mcp`, and click **Connect** on the Google Calendar card. Re-consent
grants the new scopes; tokens are stored encrypted and refreshed automatically.

## License

MIT
