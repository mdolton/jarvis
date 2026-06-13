# Dashboard MCP Server Management — Design

**Date:** 2026-06-13
**Status:** Approved for planning

## Goal

Let an operator add, enable, disable, and remove MCP servers from the Dashboard's
MCP tab, covering both OAuth-backed servers and traditional HTTP-stream / SSE
servers — without editing files or restarting the process. Along the way, move
the OAuth provider catalog and all credentials out of code and environment
variables and into the database, and restructure the model so a single service
("Google Calendar") can host multiple independent account connections.

## Background

Today (pre-change):

- **Traditional MCP servers** are declared in `config/mcp-servers.yaml`, validated
  by the Pydantic `MCPServerConfig` (`jarvis/config/schema.py`), and loaded
  read-only at startup. Transports: `stdio`, `http`, `sse`.
- **OAuth providers** are a hardcoded dict, `OAUTH_CATALOG` in
  `jarvis/oauth/catalog.py` (Gmail, Google Calendar, Fastmail). Every OAuth
  operation in `jarvis/oauth/flow.py` and `jarvis/mcp/manager.py` keys off
  `OAUTH_CATALOG[provider_key]`.
- **Credentials**: `OAuthCredentialsRow` (one row per provider key) conflates the
  OAuth *app* credentials (`client_id`/`client_secret`) with the *account* tokens
  (`access_token`/`refresh_token`). MANUAL-mode app credentials (Google) are read
  from `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` env vars.
- **Runtime**: `MCPManager` owns the live connection lifecycle on a single owner
  task (`_lifecycle_loop`) processing `connect_cfg` / `replace` / `remove` /
  `shutdown` commands. This single-owner invariant is required because the
  streamable-HTTP SDK is built on anyio, whose cancel scopes must be entered and
  exited on the same task. `MCPServerRow` / `MCPToolRow` mirror runtime status and
  tool inventory; the MCP tab renders them read-only plus a per-tool policy form.
- The dashboard is FastAPI + Jinja2 + HTMX; actions are form POSTs returning
  `303 → /mcp`. Schema is managed by Alembic (`alembic/versions/`, currently
  through `0010`) with an idempotent seed pattern (`seed_built_in_digest_templates`).

The dashboard can currently view servers and OAuth cards and change tool policy,
but cannot create, enable, disable, or remove servers, and cannot hold more than
one account per OAuth provider.

## Key decisions

1. **Source of truth — hybrid, leaning DB.** stdio servers remain file-declared
   in `mcp-servers.yaml`. Everything else (OAuth and credentialed http/sse) moves
   into the database. The hardcoded catalog becomes seed data only.
2. **Catalog / connection split.** A **Provider** is a reusable, secret-free
   service *definition*. A **Connection** is one credentialed *instance* of a
   provider (one account). A provider may have many connections; each enabled
   connection becomes one live MCP server.
3. **Providers hold no secrets and nothing account-variable.** OAuth client
   credentials and scopes live on the connection (different accounts use different
   OAuth apps and may request different scopes). The provider holds only auth-server
   protocol facts and non-authoritative form-prefill hints.
4. **Credentials leave the environment.** App credentials live encrypted on the
   connection. A one-time migration imports `GOOGLE_OAUTH_CLIENT_ID/SECRET` from env
   into the seeded Google connections; env is never read at runtime again.
5. **Dashboard creates http/sse + OAuth only.** stdio creation from the web UI is
   refused at both the route and schema layers (it would be arbitrary local command
   execution from an unauthenticated web surface). stdio servers stay YAML-declared
   and are only enable/disable-toggleable from the dashboard.
6. **Runtime mirror unchanged in role.** `MCPServerRow` / `MCPToolRow` remain the
   single runtime status + tool mirror for *every* live server, keyed by `name`.

## Architecture

### Concepts

| Concept | Storage | Holds | Cardinality |
|---|---|---|---|
| **Provider** | `mcp_providers` (DB) | Service definition; no secrets | Catalog entry |
| **Connection** | `mcp_connections` (DB) | Per-account credentials, scopes, tokens, label, enabled | N per provider |
| **stdio server** | `mcp-servers.yaml` (file) | Local command + env | File-declared |
| **Runtime mirror** | `MCPServerRow` / `MCPToolRow` (DB) | Live status + tool inventory for every connected server | 1 per live server |

A live MCP server is produced by either an enabled connection or a stdio YAML
entry. Each is registered in the manager under a unique `name`: a connection's
`runtime_name` (e.g. `calendar:work`) or the stdio YAML `name`.

### Data model

**`mcp_providers`** — pure definition, no secrets:

- `key` (PK, slug), `display_name`, `kind` (`oauth` | `http` | `sse`), `mcp_url`,
  `builtin` (bool)
- oauth protocol facts (properties of the auth server, invariant across accounts):
  `auth_mode` (`dcr` | `manual`), `oauth_metadata_url`, `pkce`,
  `send_resource_indicator`, `extra_auth_params_json`
- non-authoritative form-prefill hints: `default_scopes_json` (catalog's documented
  scope set), `header_names_json` (header names a credentialed http/sse connection
  must supply)
- timestamps

`builtin` providers (Gmail, Google Calendar, Fastmail) have locked definition
fields and cannot be deleted; user-created providers are fully editable and
removable.

**`mcp_connections`** — one credentialed instance (account) of a provider:

- identity: `id` (PK, uuid), `provider_key` (FK → `mcp_providers.key`), `label`,
  `runtime_name` (unique), `enabled` (bool)
- oauth client credentials (Fernet-encrypted, nullable): `client_id_enc`,
  `client_secret_enc` — operator-supplied (MANUAL) or the per-connection DCR
  registration result
- `scopes_json` — the effective scopes requested for *this* connection
  (form-prefilled from the provider's `default_scopes_json`; the connection is
  authoritative)
- oauth tokens (encrypted, nullable): `access_token_enc`, `refresh_token_enc`,
  `token_expires_at`, `scopes_granted_json`
- http/sse: `url_override` (nullable), `headers_enc` (encrypted JSON of header
  values, e.g. an API token)
- timestamps

`runtime_name` is derived once at creation as `f"{provider_key}:{slug(label)}"`
and stored, guaranteeing a stable, unique manager key even if the label is later
edited. Runtime status (`connected` / `disconnected` / `needs_reauth` / `error`)
is **not** stored on the connection — it is read from the joined `MCPServerRow`,
which keeps a single source of runtime truth.

**`mcp_pending`** — renamed from `oauth_pending`; `state` (PK), `connection_id`
(FK, replaces `provider_key`), `code_verifier`, `created_at`.

**`MCPServerRow` / `MCPToolRow`** — unchanged in role and structure. Add a nullable
`connection_id` FK and a `source` column (`stdio` | `connection`) to `MCPServerRow`
for clean joins between desired-state and runtime. Tools continue to hang off
`MCPServerRow`, so the existing per-tool policy UI is untouched.

**Retired:** `OAuthCredentialsRow` — its app-credential half moves to the
connection's `client_id_enc`/`client_secret_enc`; its token half moves to the
connection's token columns. The migration converts existing rows before dropping
the table.

### DB-backed catalog

- The hardcoded `OAUTH_CATALOG` dict is renamed to `SEED_PROVIDERS` and used
  **only** by the `0011` migration and an idempotent `seed_built_in_providers()`
  bootstrap step (mirroring `seed_built_in_digest_templates`). Runtime reads
  exclusively from `mcp_providers`.
- A new `ProviderCatalog` service exposes `get(key) -> ProviderEntry` and
  `list() -> list[ProviderEntry]`, reconstructing the existing `ProviderEntry`
  shape from `mcp_providers` rows. It decrypts **no** secrets (there are none on
  the provider). `_resolve_manual_client`'s `os.environ` lookup is deleted; client
  credentials are read from the connection at auth time.

### OAuthFlow rekey: provider_key → connection_id

Every method takes a connection and resolves `connection → provider` for the
service definition, while reading and writing credentials/tokens on the
**connection**:

- `start_authorization(connection_id)` — resolve provider for endpoints/metadata/
  `auth_mode`; read client creds + scopes from the connection. For DCR, register a
  client if the connection has none and persist the result onto the connection. For
  MANUAL, use the connection's stored client creds. Generate PKCE/state and insert
  a `mcp_pending` row keyed by `connection_id`. The "registered but not authorized"
  sentinel (empty access token) now lives on the connection.
- `handle_callback(state, code)` — look up pending by state, resolve
  `connection → provider`, exchange the code, write tokens onto the connection,
  delete the pending row.
- `refresh(connection_id)` / `revoke(connection_id)` / `current_headers(connection_id)`
  — all per connection.
- The scheduler's `oauth_token_refresh` job iterates **connections** due for
  refresh and applies the new token to the live connection by `runtime_name`.

### Manager rekey: name → connection runtime_name

- `start()` connects YAML stdio servers (unchanged) **plus** every enabled
  connection: oauth connections build a token-holder streamable-HTTP server with
  the current access token (skipped when the connection has no tokens yet or is in
  `needs_reauth`); http/sse connections build a streamable-HTTP server from the
  stored `url_override`/`headers`. Live-server dicts (`_stacks`, `_sdk_servers`,
  `_tool_names_by_server`) and `_token_holders` are all keyed by `runtime_name`.
- Add thin public methods `connect_connection(conn)` / `disconnect(name)` that wrap
  the existing generic `connect_cfg` / `remove` lifecycle commands (the `remove`
  handler is already generic by name). The owner-task / anyio invariant is
  preserved — no new command paths that enter/exit stacks off the owner task.
- `update_oauth_token` / `refresh_oauth_server_for_retry` / the `replace` path key
  by `runtime_name` and resolve the provider via the connection.

### Config / schema

- `mcp-servers.yaml` schema is restricted to `transport: stdio`. An `http`/`sse`
  entry raises a clear validation error directing the operator to add it via the
  dashboard. The file is currently empty, so there is nothing to migrate out.
- `assert_no_yaml_collision` now checks stdio YAML names against provider keys and
  connection `runtime_name`s.

## Dashboard (mcp.html, HTMX)

Three sections, all form POSTs returning `303 → /mcp`, audit events via `ctx.audit`:

- **Providers** — catalog list. "Add provider" (oauth or http/sse: key,
  display_name, mcp_url, and for oauth the auth-server fields). Edit user providers;
  builtin providers show locked definition fields. Remove user providers
  (cascade-guards: refuse or cascade to connections — see Open question 1).
- **Connections** — grouped under each provider. "Add connection" (label + provider;
  for oauth optionally client creds + scopes prefilled from provider defaults; for
  http/sse the header values + optional url override). Per connection:
  Connect / Disconnect / Reconnect (oauth) or Save-and-connect (http/sse),
  Enable / Disable, Remove. Live status + tool inventory rendered from the joined
  `MCPServerRow`.
- **stdio servers** — file-managed list. Enable / Disable toggle only (the override
  is persisted in DB and survives restart); no create or remove from the UI.

## Migration

Alembic `0011_provider_connection_model`:

1. Create `mcp_providers`, `mcp_connections`; rename `oauth_pending` →
   `mcp_pending` with `provider_key` → `connection_id`; add `connection_id` +
   `source` to `mcp_servers`.
2. Seed builtin providers from `SEED_PROVIDERS` (definitions only, `builtin=true`).
3. For each existing `oauth_credentials` row: create one `default` connection for
   the matching provider, moving the app credentials and tokens onto it, and
   setting its scopes from the catalog default.
4. Import `GOOGLE_OAUTH_CLIENT_ID/SECRET` from env (if set) into the Gmail and
   Calendar `default` connections, encrypted.
5. Drop `oauth_credentials`.

`seed_built_in_providers()` runs idempotently at bootstrap (alongside the existing
template seed) so fresh databases created via `create_all` also get the catalog.

## Phasing

One spec, two implementation phases:

- **Phase 1 — foundational refactor (behavior-preserving).** New data model,
  `ProviderCatalog`, `OAuthFlow`/manager/scheduler rekey, migration + seed. End
  state: Gmail / Calendar / Fastmail work exactly as before, now DB-backed, each
  with a single `default` connection. No new UI. This is the risky part; it ships
  green with unchanged externally-visible behavior.
- **Phase 2 — dashboard management.** Providers / connections / stdio CRUD UI and
  routes: add http/sse and arbitrary OAuth providers, add multiple connections per
  provider, enable / disable / remove, edit app credentials and scopes.

## Testing

Existing patterns: stubbed `httpx` (`MockTransport`) and in-memory DB.

- Repository tests: provider and connection CRUD, `runtime_name` uniqueness.
- `ProviderCatalog`: `ProviderEntry` reconstruction from rows; `list`/`get`
  precedence and builtin flags.
- `OAuthFlow` rekey: multi-connection isolation — two connections on one provider
  authorize, refresh, and revoke independently without cross-contaminating tokens
  or clients; per-connection DCR registration; MANUAL creds read from the connection.
- Migration/seed: `oauth_credentials` → provider + `default` connection conversion;
  env-import idempotency; builtin seed idempotency on re-run.
- Manager: multi-connection connect/disconnect on the owner task; `runtime_name`
  keying; token-holder update by connection.
- Schema: stdio-only YAML validation rejects http/sse; collision check covers
  provider keys and runtime names.
- Routes: each CRUD path (add/edit/enable/disable/remove provider, connection, and
  stdio toggle), including stdio-creation refusal.

## Open questions (resolve during planning)

1. **Removing a provider that still has connections** — refuse with a message
   ("remove its connections first") vs. cascade-delete connections (disconnect +
   revoke each). Leaning refuse for safety.
2. **`headers_enc` encryption granularity** — encrypt the whole header JSON blob
   (simpler) vs. per-value. Leaning whole-blob, consistent with token encryption.

## Out of scope

- Authentication / authorization on the dashboard itself (unchanged; still
  same-origin only).
- Editing stdio server definitions from the UI (they remain file-managed).
- stdio creation from the dashboard.
