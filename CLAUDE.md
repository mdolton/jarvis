# Jarvis

Self-hosted personal AI agent. A single-process async Python app: connects to any
OpenAI-compatible LLM, gains capabilities through MCP servers, talks over Discord,
runs scheduled tasks via APScheduler, persists to SQLite, and serves a FastAPI +
HTMX dashboard. Runs in Docker.

## Commands

Use `uv` for everything; there is no activated venv.

| Task | Command |
|------|---------|
| Tests | `uv run pytest -q` (or `make test`) |
| Single test | `uv run pytest tests/integration/test_x.py::test_y -q` |
| Lint | `uv run ruff check jarvis tests` (`make lint`) |
| Format / autofix | `make fmt` (`ruff check --fix` + `ruff format`) |
| Lint + tests | `make check` — run before deploying |
| Apply migrations | `uv run alembic upgrade head` |
| New migration | `uv run alembic revision --autogenerate -m "msg"` |
| Run migration on a scratch DB | `uv run alembic -x db_url=sqlite+aiosqlite:////tmp/x.db upgrade head` |
| Run app locally | `uv run python -m jarvis serve` (Discord + scheduler + dashboard on :8080) |
| One-shot agent run | `uv run python -m jarvis invoke "..."` |
| Validate config | `uv run python -m jarvis check-config` |
| Local docker stack | `make up` / `make down` / `make logs` |

Python 3.12 only (`requires-python >=3.12,<3.13`). Ruff: line-length 100, target py312
(`ruff.toml`). Pytest runs in `asyncio_mode = auto` (`pytest.ini`) — write `async def test_*`
with no `@pytest.mark.asyncio`.

The Docker entrypoint runs `alembic upgrade head` then `python -m jarvis serve`.

## Layout

- `jarvis/main.py`, `cli.py` — bootstrap (`AppContext`) and Typer CLI (`invoke`, `check-config`, `serve`).
- `jarvis/persistence/` — `db.py` (async engine, `TZDateTime`), `models.py` (all ORM rows), `repositories.py` (typed repos — **the only way to touch the DB**).
- `jarvis/mcp/manager.py` — MCP server lifecycle (connect/replace/stop, tool discovery).
- `jarvis/oauth/` — `flow.py` (DCR, authz, token refresh), `store.py` (`MCPProviderRepo`, `MCPConnectionRepo`), `catalog.py` (built-in provider seed), `crypto.py` (Fernet), `discovery.py` (auto-detect OAuth metadata from an MCP URL).
- `jarvis/scheduler/` — APScheduler wrapper + `oauth_jobs.py` (background token refresh).
- `jarvis/agents/` — Agents-SDK runner, LLM client, model catalog/selection.
- `jarvis/channels/` — Discord adapter + slash commands.
- `jarvis/memory/` — preferences + vector recall (sqlite-vec), semantic dedup.
- `jarvis/web/` — FastAPI app, `routes/`, Jinja templates, static assets.
- `jarvis/core/` — `dispatcher.py` (concurrency/dedup/allow-list), `types.py`, output routing.
- `alembic/versions/` — numbered migrations `0001…`. `config/*.yaml.example` — config templates.
- `tests/unit/` (mocked) and `tests/integration/` (real SQLite). Integration tests build a
  schema with `Base.metadata.create_all` or by running real alembic via subprocess; a conftest
  autouse fixture sets `JARVIS_SECRETS_KEY`.

## Conventions & gotchas

- **Persistence goes through repositories**, never raw sessions in feature code. DB is SQLite
  with WAL + `PRAGMA foreign_keys=ON` at runtime (`db.py`).
- **Datetimes use `TZDateTime`** (stores naive UTC, returns aware UTC). All `Mapped[datetime]`
  values are timezone-aware UTC; binding a naive datetime raises.
- **MCP single-owner-task invariant**: `MCPServerStreamableHttp` uses anyio cancel scopes that
  must be entered AND exited on the same asyncio task. All connect/replace/close/refresh ops are
  funnelled through one lifecycle task in `mcp/manager.py`. Closing a connection on a different
  task corrupts anyio state and tears down the event loop. See
  `tests/integration/test_mcp_manager_lifecycle.py`.
- **Migrations write UUIDs as `uuid4().hex`, never `str(uuid4())`.** SQLAlchemy's `Uuid` type
  stores/queries UUIDs on SQLite as 32-char hex (no dashes). A dashed value parses on full-table
  scans but every `session.get` (PK lookup) misses it → silent "no row". This caused the OAuth
  "no connection row" bug (fixed in `0011` + repair migration `0012`).
- **Foreign keys are OFF during alembic migrations** (env.py uses its own engine, not the app's),
  so parent/child columns can be normalized independently in a data migration.
- **SQLite DDL is non-transactional**: a failed migration leaves partial state behind. Use
  separate `batch_alter_table` blocks when a column add + default-drop must each recreate the
  table (see `0011`'s `source` backfill).
- **MCP servers are two kinds**: stdio servers from `config/mcp-servers.yaml` (version-controlled,
  toggled via the `mcp.stdio_disabled` setting) vs. provider/connection rows managed from the `/mcp`
  dashboard (`source='connection'`). `MCPManager.start()` prunes orphaned `source='stdio'` rows not
  in the yaml.
- **Secrets at rest**: OAuth tokens, client secrets, and HTTP auth headers are Fernet-encrypted
  with `JARVIS_SECRETS_KEY`; decrypt only at point of use.
- **Built-in providers** (seeded in migration `0011`, mirrored in `oauth/catalog.py`, kept in sync
  by `test_migration_seed_matches_catalog`): Fastmail (DCR), Gmail and Google Calendar (manual auth).
- **Gmail tool 403s** are usually external, not a code bug: `gmailmcp.googleapis.com` is behind an
  early-access allowlist and the failure can mean the wrong Google account is connected.
- **OAuth provider discovery**: `oauth/discovery.py` `discover_provider(mcp_url, http)` derives the
  `oauth_metadata_url`, `auth_mode` (`dcr` if the AS advertises `registration_endpoint`, else `manual`),
  and scopes from just the MCP URL — RFC 9728 protected-resource metadata (MCP path → origin) →
  `WWW-Authenticate` hint → AS-at-origin fallback, then RFC 8414 / OIDC metadata. It never raises on a
  miss (returns a `DiscoveryResult` with a `notes` trace); the `/mcp/providers/discover` HTMX route
  prefills the Add Provider form via out-of-band swaps. Operator-initiated only (no SSRF allow-listing).

## Workflow

Branch off `main`, open a PR (don't push to `main`). Co-author trailer on commits.
`make check` must be green before deploy. Migration changes need a test under
`tests/integration/` that runs real alembic (see `test_migration_0011.py`,
`test_connection_uuid_repair_migration.py`).
