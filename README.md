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
reverse proxy and forward to the container's port 8080. If the dashboard is
reachable outside a trusted network, enforce authentication at the reverse proxy;
Jarvis blocks cross-origin unsafe requests but does not provide user login.

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
| `mcp-servers.yaml` | Local **stdio** MCP server definitions (HTTP/SSE & OAuth servers are managed in the dashboard) |

Environment variables in YAML files are expanded via `${VAR}` syntax. Secrets should go in `.env` (never committed) and be referenced in YAML.

## MCP Servers

Jarvis gains capabilities by connecting to [MCP](https://modelcontextprotocol.io/) servers, configured two ways:

- **Local stdio servers** live in `mcp-servers.yaml`. They launch a command on the
  Jarvis host, so they stay in version-controlled config rather than the web UI:

  ```yaml
  servers:
    - name: filesystem
      transport: stdio
      command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"]
  ```

  You can enable/disable a file-declared stdio server from the dashboard without
  editing the file (the toggle is persisted), but stdio servers can only be
  **created** by editing `mcp-servers.yaml` — this keeps arbitrary local-command
  execution off the web surface.

- **Remote (HTTP/SSE) and OAuth servers** are added and managed entirely from the
  `/mcp` dashboard and stored in the database. See
  [MCP providers & connections](#mcp-providers--connections) below.

MCP tools can be marked `allow`, `confirm`, or `deny` from the dashboard.
Read-like tools auto-run by default; side-effecting tools pause in the Action
Inbox until approved.

## Scheduled Tasks

Create schedules via the dashboard at `/schedules`. Each schedule has:
- A cron expression (e.g., `0 8 * * *` for 8am daily)
- A prompt (what to tell the agent)
- An output mode: `discord` (DM you), `dashboard_only` (silent), or `discord_if_noteworthy` (agent decides)
- An optional Discord user ID for scheduled output and error notifications
- A **Run now** action for manually firing a schedule from the dashboard

Digest templates are available from the **Templates** page. Built-in templates
include Daily Brief, Email Digest, Calendar Brief, and Action Inbox Review.
Creating a schedule from a template copies the template fields into the
schedule; future template edits do not change existing schedules.

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

### Long-term memory and preferences

Jarvis has two separate memory lanes:

- **Preferences** are approved standing instructions that shape future behavior.
  Pending preference proposals appear on the Memory page and only active
  preferences are injected into runs.
- **Recall memories** are compact summaries of prior conversations. Jarvis
  embeds summaries with `sqlite-vec`, searches them automatically across
  Discord, dashboard, and scheduled runs, and injects relevant memories as
  prior context. Raw transcripts remain available for exact recall requests,
  but Jarvis does not embed every raw message in v1.

Memory config lives in `jarvis.yaml`:

```yaml
memory:
  enabled: true
  recall_enabled: true
  embedding_model:
  embedding_dimensions: 1536
  max_recalled_memories: 5
  min_relevance_score: 0.25
```

If `sqlite-vec` cannot load, Jarvis continues running with preferences enabled
and automatic vector recall disabled.

## Dashboard

Available at `http://localhost:8080` when running:

- **Home** — service status overview, component diagnostics, and manual prompt runs
- **Conversations** — browse past agent interactions
- **Memory** — approved preferences, recall summaries, evidence snippets, and recall debugging
- **Schedules** — create, run, enable/disable, delete schedules
- **Templates** — create, edit, clone, and apply reusable digest templates for schedules
- **MCP** — manage providers and per-account connections (add/remove, enable/disable, connect/disconnect), browse discovered tools, and set per-tool policy overrides
- **Actions** — approve or reject MCP tool calls that require confirmation before execution
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

## MCP providers & connections

The `/mcp` dashboard manages remote MCP servers with a **provider / connection** model:

- A **provider** is a reusable, secret-free definition of a service — its MCP URL,
  transport, and (for OAuth) its auth-server details. The catalog lives in the
  database and ships with three built-in providers: **Fastmail**, **Gmail**, and
  **Google Calendar**. You can add your own OAuth (DCR or manual) or HTTP/SSE
  providers from the dashboard.
- A **connection** is one credentialed instance of a provider — i.e. one account.
  A single provider can have **multiple connections** (e.g. a personal and a work
  Google account), each with its own credentials, scopes, tokens, and live MCP
  server. Connections are added, enabled/disabled, connected/disconnected, and
  removed from the dashboard.

All secrets — OAuth client IDs/secrets, access/refresh tokens, and HTTP headers —
are encrypted at rest with a Fernet key and stored on the **connection**. The
provider catalog holds no secrets, and credentials no longer need to live
permanently in your environment.

### One-time setup (all OAuth providers)

1. Generate a Fernet secrets key:
   ```bash
   uv run python -c "from jarvis.oauth.crypto import generate_key; print(generate_key())"
   ```
2. Set the base env vars (in `.env` for Docker, or your shell for local dev):
   ```
   JARVIS_SECRETS_KEY=<paste-the-key>
   JARVIS_BASE_URL=https://jarvis.moltonlava.online   # or http://localhost:8080 locally
   ```

### Fastmail

Fastmail supports Dynamic Client Registration, so there's no manual client setup:
open `/mcp`, **Add connection** to the **Fastmail** provider, then click **Connect**.

### Gmail & Google Calendar

Google doesn't support DCR, so you create an OAuth client by hand and supply its
`client_id`/`client_secret` to the connection. Gmail and Calendar share a single
Google Cloud OAuth client (one Web-application client can request any scopes), so
you set it up once and reuse it for both. In Google Cloud Console:

1. Enable the APIs and request early access:
   - **Gmail API** + the **Gmail MCP server** (early access), and/or
   - **Google Calendar API** (`calendar-json.googleapis.com`) + the **Google Calendar
     MCP API** (`calendarmcp.googleapis.com`, part of the Google Workspace Developer
     Preview Program — enroll your project there first).
2. Configure the **OAuth consent screen** with the scopes you want:
   - Gmail: `https://www.googleapis.com/auth/gmail.readonly`,
     `https://www.googleapis.com/auth/gmail.compose`
   - Calendar: `https://www.googleapis.com/auth/calendar.calendarlist.readonly`,
     `https://www.googleapis.com/auth/calendar.events.freebusy`,
     `https://www.googleapis.com/auth/calendar.events.readonly`

   While the app is unverified, add your Google account as a **test user**.
3. Create an **OAuth client ID** of type **Web application**:
   - **Authorized redirect URI:** `${JARVIS_BASE_URL}/oauth/callback`
     (e.g. `https://jarvis.moltonlava.online/oauth/callback`) — must match exactly.
   - **Authorized JavaScript origins:** leave empty — Jarvis uses a server-side code flow.

Then connect, supplying the client credentials one of two ways:

- **From the dashboard (recommended):** open `/mcp`, **Add connection** to the
  **Gmail** (or **Google Calendar**) provider, paste the `client_id`/`client_secret`
  and adjust scopes in the form, then click **Connect**. Add a second connection for
  another account whenever you like.
- **From the environment (convenience):** set
  ```
  GOOGLE_OAUTH_CLIENT_ID=<from Google Cloud Console>
  GOOGLE_OAUTH_CLIENT_SECRET=<from Google Cloud Console>
  ```
  These are imported **once** into a default Gmail and Calendar connection on first
  startup; afterwards the credentials live encrypted in the database and the env vars
  are no longer read.

Jarvis stores the encrypted tokens and refreshes them automatically. Re-consent on a
connection grants any newly-added scopes; if a refresh permanently fails the
connection shows **Needs re-auth** with a Reconnect button.

### Adding other providers

Use **Add provider** on `/mcp` to register any other MCP service:

- **OAuth (DCR)** — supply the MCP URL and its OAuth metadata URL; Jarvis registers a
  client automatically per connection.
- **OAuth (manual)** — for providers without DCR; enter the client credentials on each
  connection, as with Gmail/Calendar.
- **HTTP / SSE** — supply the URL and any auth headers (e.g. a bearer token) on the
  connection; headers are encrypted at rest.

## License

MIT
