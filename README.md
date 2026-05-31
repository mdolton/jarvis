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

## OAuth-protected MCP servers

Jarvis connects to OAuth-protected HTTP MCP servers from the `/mcp` dashboard. Two
providers ship in the catalog:

- **Fastmail** — Dynamic Client Registration (DCR); no manual client setup.
- **Gmail** — Google's official Gmail MCP server (`https://gmailmcp.googleapis.com/mcp/v1`).
  Google does not support DCR, so you create the OAuth client by hand and supply its
  credentials via environment variables.

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

## License

MIT
