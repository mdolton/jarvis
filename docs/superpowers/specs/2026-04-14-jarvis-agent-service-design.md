# Jarvis — Personal AI Agent Service (Design)

**Status:** Approved design, pending implementation plan.
**Date:** 2026-04-14
**Scope:** v1 ("thin slice") — minimum system that proves the full architecture end-to-end.

## 1. Overview

Jarvis is a personal AI agent service that runs on a Linux server via Docker Compose. It reaches any OpenAI-compatible LLM (LM Studio, Ollama, remote endpoints), gains capabilities by connecting to MCP servers, is reachable via Discord DM, and can invoke itself on cron schedules. A web dashboard exposes configuration, live activity, and a complete audit log of every action the agent takes.

v1 is a **thin slice**: every subsystem is in place, but each is deliberately minimal so the end-to-end architecture is validated before breadth is added. Features explicitly out of v1 are listed in §11.

## 2. Goals and non-goals

**Goals**
- Single-user personal agent, deployed as a single Docker Compose service.
- Agent capabilities come from MCP servers. No other extension mechanism in v1.
- Works with any OpenAI-compatible LLM endpoint.
- One messaging channel in v1: Discord DM.
- Scheduled ("cron-like") self-invocations with configurable output destinations.
- Complete, forensic-quality audit log of every LLM call, tool call, and agent decision.
- Web dashboard for configuration, live monitoring, and log review.
- Clean extension points so additional channels (Slack, WhatsApp) and richer memory can be added without rewrites.

**Non-goals (v1)**
- Multi-user / tenant isolation.
- Long-term memory, vector stores, RAG.
- Human-in-the-loop confirmation for tool calls (autonomous in v1; see §11).
- Web-based editing of MCP server configuration (YAML only in v1).
- Multiple messaging channels simultaneously.
- Authentication on the dashboard (assume trusted local network or reverse proxy handles this).

## 3. Key decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Runtime model | Hybrid ephemeral: agent runs are short-lived, memory lives in the DB | Survives restarts cleanly, no process-state concerns, matches modern agent framework designs |
| Language / stack | Python 3.12 | Strongest agent + MCP ecosystem |
| Agent framework | OpenAI Agents SDK (`openai-agents`) | Purpose-built for OpenAI-compatible endpoints; native MCP support; built-in tracing we can tap for audit |
| Extension mechanism | MCP servers only | Simplicity; anything "skill-like" is built as a specialized MCP server |
| v1 channel | Discord (via `discord.py`) | Gateway-based (no public webhook URL), mature library, clean for single-user DMs |
| Memory model | Session-scoped (idle timeout closes thread) | Simplest; richer memory deferred to v2 — via MCP memory server if desired |
| Autonomy model | Autonomous in v1, per-tool policy module pre-built for v2 | Confirmation flow deferred, but `ToolPolicy` module lands now so v2 is a UI wiring job |
| Scheduler | APScheduler in the same event loop | Personal scale; avoids Celery/Redis |
| Scheduler output | Per-schedule: `discord` \| `dashboard_only` \| `discord_if_noteworthy` (default `discord`) | Flexible without being heavy |
| Dashboard stack | FastAPI + Jinja2 + HTMX + SSE | One Python process, minimal JS, easy to extend |
| Persistence | SQLite (WAL mode), SQLAlchemy + Alembic | Single-node scale; zero-ops deployment |
| Config | YAML for MCP servers/channels/LLM; dashboard for schedules | File-based for infra, UI for user-facing entities |
| Deployment | Single Docker Compose service, bind-mounted `./data` and `./config` | Simple local ops; `host.docker.internal` for local LLM endpoints |

## 4. Architecture

### 4.1 High-level component map

```
Inputs ─────────────────────────────────────────────────────────────────
  DiscordAdapter    Scheduler       Dashboard "Run Now"
            \           |                 /
             \          ↓                /
              ─→  TriggerDispatcher ←─
                        |
                        ↓
                  AgentRunner ─────────→ LLMClient (OpenAI-compatible)
                        |
                        ├────────→ MCPManager ─→ (N × MCP servers)
                        |
                        ↓
                  OutputRouter ──→ Channels / Dashboard feed

Observability ──────────────────────────────────────────────────────────
  AuditLogger ←── (every component writes events here)
         ↓
   SQLite (audit_events, conversations, messages, triggers, schedules, ...)
         ↑
  WebDashboard ───→ reads DB, serves HTML + SSE
```

### 4.2 Process topology

Single Python process, asyncio event loop. `discord.py`, APScheduler, FastAPI (via uvicorn), and the MCP clients are all cooperative tasks in one process. No message broker, no worker pool, no separate scheduler daemon. Concurrency limits are enforced in `TriggerDispatcher` (default: max N concurrent agent runs).

### 4.3 Module layout

```
jarvis/
  core/
    agent_runner.py       # AgentRunner
    dispatcher.py         # TriggerDispatcher, InvocationRequest
    models.py             # Pydantic types: InvocationRequest, AuditEvent, etc.
  channels/
    base.py               # ChannelAdapter protocol
    discord_adapter.py
  mcp/
    manager.py            # MCPManager
    tool_policy.py        # read/write classification (used by v1 audit, v2 confirm)
  scheduler/
    scheduler.py          # APScheduler wrapper
  persistence/
    db.py                 # SQLAlchemy engine, session
    repositories.py       # ConversationRepo, AuditRepo, ScheduleRepo, ...
    models.py             # SQLAlchemy ORM models
  audit/
    logger.py             # AuditLogger
    tracer.py             # Agents SDK tracing → AuditLogger bridge
  web/
    app.py                # FastAPI app
    routes/               # Route handlers
    templates/            # Jinja2 templates
    static/
  config/
    loader.py             # YAML loader + validation
    schema.py             # Pydantic config schemas
  main.py                 # Wires everything, starts the event loop
```

### 4.4 Boundary rules

- **Core components use repositories, not ORM models.** `AgentRunner` sees `ConversationRepo`, not `SQLAlchemy` sessions. Keeps tests easy and swapping SQLite for Postgres a non-event.
- **Every observable step writes an audit event.** No silent paths. Failures always produce events.
- **`OutputRouter` is the only thing that sends outbound messages.** Components never call channel adapters directly; they hand the result to the router, which knows the trigger's output config.
- **`ToolPolicy` is already a module in v1.** Even though all tools execute autonomously in v1, the classification (annotation → heuristic → override) is computed and stored on every `tool.call` audit event. This makes the v2 confirmation flow a UI change, not a design change.

## 5. Components

### 5.1 `AgentRunner` (`core/agent_runner.py`)
Given an `InvocationRequest`, starts an Agents SDK run, streams events, returns the final output. Stateless; depends on injected repositories, `MCPManager`, `LLMClient`, `AuditLogger`. Responsibility ends at "agent produced an output" — routing is handled elsewhere.

### 5.2 `TriggerDispatcher` (`core/dispatcher.py`)
The only creator of `InvocationRequest`s. Applies cross-cutting policy:
- Authorization (Discord user allow-list, schedule enabled check).
- Concurrency gate (max N concurrent runs; excess queued or rejected per config).
- Deduplication (in-memory LRU of seen Discord message IDs; protects against gateway retries).
- Conversation lookup/creation (by `(channel_kind, channel_ref)`, honoring idle timeout).

### 5.3 `DiscordAdapter` (`channels/discord_adapter.py`)
Implements `ChannelAdapter` protocol. Connects to Discord via the gateway, subscribes to DM events from allow-listed user IDs, pushes into `TriggerDispatcher`, sends outbound messages via `OutputRouter`. Handles reconnects (discord.py's built-in), logs connection lifecycle to audit.

### 5.4 `ChannelAdapter` protocol (`channels/base.py`)
```python
class ChannelAdapter(Protocol):
    kind: str  # "discord", "slack", ...
    async def start(self, dispatcher: TriggerDispatcher) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, channel_ref: str, text: str) -> None: ...
```
Future Slack/WhatsApp adapters implement this interface. No other parts of the system need to change.

### 5.5 `MCPManager` (`mcp/manager.py`)
Reads MCP server configs from `mcp-servers.yaml`, establishes persistent client connections (stdio, http, or sse transports per server), maintains a live cache of tool metadata (name, description, schema, annotations), and serves the Agents SDK as a tool source. Background reconnect loop with exponential backoff; tools from disconnected servers are marked unavailable in the catalog rather than failing at call time when possible.

### 5.6 `ToolPolicy` (`mcp/tool_policy.py`)
Single pure function `classify(tool: MCPTool) -> ToolPolicy`. Precedence:
1. User override (from `mcp_tools.policy_override` in DB) — wins if set.
2. MCP annotations: `readOnlyHint: true` → `auto`; `destructiveHint: true` → `confirm`.
3. Heuristic on tool name: `get_*`, `list_*`, `read_*`, `search_*`, `fetch_*` → `auto`; everything else → `confirm`.
v1 records the classification on every `tool.call` audit event but always executes. v2 will consult it to drive a Discord confirmation prompt.

### 5.7 `Scheduler` (`scheduler/scheduler.py`)
Thin wrapper over APScheduler. On startup, loads enabled schedules from SQLite and registers jobs. Dashboard CRUD calls into it to add/remove/modify jobs live without a restart. Each fire produces a `ScheduledTrigger` → `InvocationRequest`. Records `last_run_at` / `last_run_status` per schedule.

### 5.8 `AuditLogger` (`audit/logger.py`)
Single sink. Takes structured `AuditEvent` objects and writes to the `audit_events` table. Async, buffered, flushed on a short interval. Consumed by the dashboard's SSE endpoint for live tailing.

### 5.9 `Tracer` (`audit/tracer.py`)
Implements the OpenAI Agents SDK tracing interface, translating SDK span events into `AuditEvent`s written via `AuditLogger`. This gives us LLM request/response, tool call/result, and agent decision events "for free."

### 5.10 `OutputRouter` (`core/`)
Given an agent's final output and the originating `InvocationRequest`, routes the output:
- Discord-triggered: reply in the same DM thread via `DiscordAdapter.send()`.
- Scheduled: consult `schedule.output_mode`. For `discord_if_noteworthy`, the schedule's prompt includes an instruction that tells the agent to prefix its reply with `[NOTEWORTHY]` or `[SILENT]`; the router honors the tag.
- Dashboard "Run Now": result displayed in-browser only.

All paths also record a `channel.sent` or `output.suppressed` audit event.

### 5.11 `WebDashboard` (`web/`)
FastAPI app. Pages:
- `/` — home/status (LLM endpoint reachable? MCP servers connected? recent activity).
- `/conversations` — list, filterable by channel kind.
- `/conversations/{id}` — transcript + audit timeline for that conversation.
- `/schedules` — CRUD.
- `/mcp` — read-only list of configured servers + their tools + connection status.
- `/audit` — raw event log, filterable by type/conversation/time range.
- `/settings` — read-only view of loaded YAML + `settings` table rows.
- `/events/stream` — SSE endpoint for live audit event tailing.
- `/healthz` — JSON health check (no auth).

No auth in v1. Deployment assumption: dashboard port is not exposed to the public internet, or is fronted by a reverse proxy that handles auth.

## 6. Data flow

### 6.1 Discord DM ("What's on my calendar today?")

1. Discord gateway delivers DM to `DiscordAdapter`.
2. Adapter checks sender allow-list, builds `ChannelMessage`, calls `TriggerDispatcher.dispatch()`.
3. Dispatcher finds or creates a `Conversation` for `(discord, <user_id>)`, honoring idle timeout.
4. User message persisted to `messages`. Audit event: `trigger.received`.
5. `AgentRunner.run(request)`. Builds system prompt, loads message history, hands off to Agents SDK Runner with `MCPManager` as tool source and the custom tracer active.
6. SDK calls LLM → LLM tool-call: `gcal.list_events(date=today)`. Audit events: `llm.request`, `llm.response`, `tool.call`.
7. `ToolPolicy.classify()` runs; stored on the `tool.call` event. (v1: always executes.)
8. MCPManager invokes tool; result to SDK. Audit event: `tool.result` (full request + response JSON).
9. SDK sends result to LLM; LLM produces final text. Audit: `llm.request`, `llm.response`.
10. Assistant message persisted. `OutputRouter` sends reply via Discord. Audit: `channel.sent`.

### 6.2 Scheduled trigger ("Every 8am summarize overnight email")

1. APScheduler fires. `Scheduler` builds `ScheduledTrigger` (prompt + output config).
2. `TriggerDispatcher` creates a fresh `Conversation` (scheduled runs never share context with DM threads). Audit: `schedule.fired`, `trigger.received`.
3. `AgentRunner.run()` — identical loop to Discord flow.
4. On completion, `OutputRouter` consults `schedule.output_mode` and routes accordingly. `schedule.last_run_at` / `last_run_status` updated.

### 6.3 Convergence property
Every trigger path converges on `AgentRunner.run(request)`. SDK does not know or care about the trigger source. Every path produces events in the same `audit_events` table, queryable uniformly.

## 7. Data model

Eight tables. SQLite (WAL mode). SQLAlchemy ORM. Alembic migrations from the start.

```
conversations
  id (uuid, pk)
  channel_kind             -- 'discord' | 'scheduled' | 'dashboard'
  channel_ref              -- discord user id, schedule id, or dashboard session id
  started_at, last_activity_at
  status                   -- 'open' | 'closed'
  idle_timeout_sec         -- per-conversation override (nullable)

messages
  id (uuid, pk)
  conversation_id (fk)
  role                     -- 'user' | 'assistant' | 'system'
  content                  -- text
  created_at
  -- NOTE: tool-call exchanges are NOT in this table; they live in audit_events

audit_events                 -- append-only, never edited
  id (uuid, pk)
  conversation_id (fk, nullable)
  trigger_id (fk, nullable)
  type                     -- 'trigger.received' | 'llm.request' | 'llm.response'
                           -- | 'tool.call' | 'tool.result' | 'tool.error'
                           -- | 'channel.sent' | 'schedule.fired'
                           -- | 'config.reload_failed' | 'output.suppressed' | ...
  payload (json)           -- full structured data
  created_at (indexed)

triggers
  id (uuid, pk)
  kind                     -- 'discord_message' | 'schedule' | 'manual'
  source_ref               -- discord message id, schedule id, user who clicked
  created_at

schedules
  id (uuid, pk)
  name, description
  cron_expr                -- '0 8 * * *'
  timezone                 -- e.g., 'America/Los_Angeles'
  prompt                   -- the text handed to the agent
  output_mode              -- 'discord' | 'dashboard_only' | 'discord_if_noteworthy'
  notify_on_error (bool)
  enabled (bool)
  created_at, updated_at
  last_run_at, last_run_status

mcp_servers                 -- shadow of YAML; source of truth is the YAML file
  id (uuid, pk)
  name, transport          -- 'stdio' | 'http' | 'sse'
  status                   -- 'connected' | 'disconnected' | 'error'
  last_error, last_connected_at

mcp_tools                   -- cached tool metadata per server
  id (uuid, pk)
  server_id (fk)
  name, description, input_schema (json)
  read_only_hint, destructive_hint  -- from MCP annotations, nullable
  policy_override          -- 'auto' | 'confirm' | null (unused in v1 execution, recorded on events)

settings                    -- key/value
  key (pk), value (json)
```

**Modeling notes**
- `triggers` as first-class enables queries like "every run caused by schedule X" without JSON parsing.
- `audit_events` is append-only; retention becomes a `settings.audit_retention_days` knob later.
- `messages` and `audit_events` are separate: messages is the human-facing transcript; audit is the forensic trail. Dashboard merges them into a timeline view.
- `mcp_servers` / `mcp_tools` are DB-cached for dashboard queryability; YAML is source of truth.

## 8. Error handling

- **LLM call failures.** SDK handles transient retry. Terminal failure → `llm.error` event, apology message to user via `OutputRouter`, conversation stays open. Next user message retries fresh.
- **MCP tool failures.** Error is returned to the LLM as a tool result; LLM decides whether to retry, try another tool, or surface to the user. Nothing is swallowed silently. `tool.error` audit event always written.
- **MCP server disconnects.** Background reconnect loop (exponential backoff). Tools from disconnected servers are marked unavailable. Reconnection state changes are audit events.
- **Discord gateway disconnects.** `discord.py` auto-reconnects; lifecycle is audit-logged. DMs arriving during disconnect are missed — accepted tradeoff (no polling fallback in v1).
- **Scheduler failures (agent errored during a cron fire).** `schedule.last_run_status = error`, full traceback to audit. If `schedule.notify_on_error = true` (default), send a Discord DM: "Your 8am task failed — [link]." Schedule stays enabled.
- **Database unavailable.** Fatal. Service exits; Docker restart policy surfaces it. No degraded mode — a working audit log is a requirement.
- **Config reload errors.** Failed YAML validation: old config stays loaded, `config.reload_failed` event, dashboard banner. No silent fallback to defaults.

**Cross-cutting rules**
- Every caught exception produces an audit event with the full traceback.
- User-visible errors say "something went wrong, check the dashboard." Internal details never leak.
- Failures between "trigger received" and "agent started" still produce a conversation + audit trail.

## 9. Testing strategy

**Unit tests (in-memory):** dispatcher logic, tool policy classifier, output router rules, config loader validation, repository methods.

**Integration tests (real SQLite, fake externals):** full `AgentRunner.run()` against a fake LLM (scripted responses) + an in-process fake MCP server (canned tools). Asserts on the audit event stream. Discord adapter against a mocked gateway. Scheduler with `freezegun` for time. Dashboard routes via FastAPI `TestClient`.

**End-to-end smoke (opt-in):** one script that runs the Compose stack against a local Ollama + a real throwaway MCP server, sends a Discord DM from a test bot, asserts a reply. Manual / release-gated, not in the default loop.

**Explicitly not tested:** real LLM output content, third-party MCP server behavior, real Discord gateway connectivity (covered only by the E2E smoke test).

**Test-first for contracts:** `AuditEvent` schema and `InvocationRequest` shape get tests before implementation; these are the contracts everything depends on.

**Tooling:** `pytest`, `pytest-asyncio`, `hypothesis` (for tool policy), `freezegun` (for scheduler).

## 10. Deployment

Single Docker Compose service. Python 3.12-slim base, `uv` for dependency install, non-root user. Embedded uvicorn serves the FastAPI app; the rest of the service runs in the same event loop.

**`docker-compose.yml` (essentials)**
```yaml
services:
  jarvis:
    build: .
    restart: unless-stopped
    ports:
      - "${JARVIS_DASHBOARD_PORT:-8080}:8080"
    volumes:
      - ./data:/app/data                    # SQLite + runtime state
      - ./config:/app/config:ro             # YAML config
      - ./mcp-servers:/app/mcp-servers:ro   # optional bundled stdio MCP servers
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

**Host layout**
```
./config/
  jarvis.yaml         # global settings
  mcp-servers.yaml    # MCP server definitions
  channels.yaml       # channel adapter config (Discord in v1)
./data/
  jarvis.db           # SQLite (WAL mode)
./.env                # secrets (Discord token, LLM API key)
```

**Secrets:** `.env` never committed; `.env.example` documents required variables. MCP server secrets pass through each server's own env in `mcp-servers.yaml` via `${VAR}` expansion.

**LLM endpoint reachability:** `host.docker.internal:host-gateway` gives the container access to host-running LM Studio / Ollama on Linux and macOS.

**Health check:** `GET /healthz` returns 200 when the event loop is responsive, DB is writable, and (if any MCP servers are configured) at least one is connected.

**Config hot-reload:** YAML files are watched. Safe updates (MCP server list, Discord allow-list) apply live. Unsafe settings (LLM base URL change) require restart and are flagged with a "restart required" badge in the dashboard.

**Logging:** structured JSON to stdout. Audit log is separate — written to SQLite, not stdout.

## 11. Out of scope for v1

Listed here so v2 planning has a clear starting point:

- **Confirmation flow for tool calls.** The `ToolPolicy` module is built and its classifications are recorded on every `tool.call` event, so v2 is a dashboard + Discord-prompt integration — not a design change.
- **Web-based MCP server configuration.** v1 uses YAML. v2 can add an admin UI that writes to YAML or promotes MCP servers to DB-managed.
- **Additional messaging channels.** `ChannelAdapter` protocol is designed to be implemented by Slack/WhatsApp/other adapters without core changes.
- **Long-term memory / RAG.** Session-scoped memory only in v1. Richer memory can land as an MCP memory server first (zero core changes), then as a first-class feature if warranted.
- **Dashboard authentication.** v1 assumes trusted network or external reverse proxy. Adding basic-auth / OIDC in v2 is a route-level concern.
- **Audit log retention.** v1 keeps everything; v2 adds `settings.audit_retention_days` + a daily cleanup job.
- **Multi-user support.** Not planned.

## 12. Open questions (resolved during brainstorm — recorded for posterity)

- Single channel for v1 → Discord (Q5).
- Autonomy for v1 → autonomous, with `ToolPolicy` pre-built (Q7).
- Memory model for v1 → session-scoped (Q6).
- Output destination for scheduled runs → per-schedule, default `discord` (Q8).
- Dashboard stack → HTMX + Jinja2 (Q9).
- Extension model → MCP only; no Claude Code-style skills (Q4).
- v1 ambition → Thin Slice (approved in scope selection).
