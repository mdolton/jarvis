# OAuth-based MCP server management

**Status:** Approved spec — ready for implementation planning
**Date:** 2026-04-25

## Goal

Let Jarvis connect to OAuth-protected HTTP MCP servers (starting with Fastmail at `https://api.fastmail.com/mcp`) without forcing the operator to hand-edit YAML, paste tokens, or restart the service after every authorization. The dashboard becomes the place where OAuth-capable MCP servers are connected, monitored, and disconnected.

v1 ships **Fastmail only** as a Dynamic Client Registration (RFC 7591) provider. The framework is designed so manual-mode OAuth (Google et al.) can be added later as catalog entries plus one new code path, without revisiting the schema or the dashboard UX.

## Non-goals (v1)

- Manual-mode OAuth providers (no DCR). Type system supports them; implementation raises `NotImplementedError`. Added when a real non-DCR HTTP MCP server arrives.
- Per-tool / per-scope authorization UI. v1 requests whatever scopes Fastmail's metadata advertises.
- Multi-user / multi-tenant. Tokens are global to the Jarvis instance. The Discord allow-list remains the only authorization layer.
- `JARVIS_SECRETS_KEY` rotation tooling. Documented manually; key change invalidates existing rows.
- OAuth-protected stdio servers — those handle their own auth.
- Polling for DCR client-metadata updates (RFC 7591 §3).
- RFC 7662 token introspection — we trust `expires_in`.

## Architecture

A new `jarvis/oauth/` package with three modules; one new dashboard route; a small extension to `MCPManager`.

```
jarvis/oauth/
  catalog.py    # frozen registry of OAuth-capable MCP providers
  flow.py       # OAuth client: discover, register, authorize, exchange, refresh, revoke
  store.py      # repository over oauth_credentials + oauth_pending tables

jarvis/web/routes/oauth.py   # GET /oauth/connect/{provider}, GET /oauth/callback

jarvis/mcp/manager.py        # extended: per-server exit stacks, replace_oauth_server, remove_oauth_server
```

### Configuration

Two new environment variables:

- `JARVIS_BASE_URL` (default `http://localhost:8080`) — used to build the OAuth redirect URI as `<base>/oauth/callback`. Both `localhost` (Google's only http exception) and TLS-fronted public URLs are supported.
- `JARVIS_SECRETS_KEY` — Fernet key for encrypting client secrets and tokens at rest. Generated and logged once if missing on first boot; operator is expected to put it in their compose env after that.

### Catalog

`jarvis/oauth/catalog.py` exports a frozen dict keyed by short provider id. Code, not YAML — adding a provider is a typed PR with a test. v1 ships one entry.

```python
class AuthMode(StrEnum):
    DCR = "dcr"          # RFC 7591 dynamic client registration
    MANUAL = "manual"    # operator-supplied client_id/secret (not implemented in v1)

@dataclass(frozen=True, slots=True)
class ProviderEntry:
    key: str                          # "fastmail"
    display_name: str
    mcp_url: str                      # streamable-http endpoint
    auth_mode: AuthMode
    oauth_metadata_url: str | None = None       # DCR mode: .well-known/oauth-authorization-server
    authorization_endpoint: str | None = None   # MANUAL mode (unused in v1)
    token_endpoint: str | None = None           # MANUAL mode (unused in v1)
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    scopes: tuple[str, ...] = ()
    pkce: bool = True

OAUTH_CATALOG: dict[str, ProviderEntry] = {
    "fastmail": ProviderEntry(
        key="fastmail",
        display_name="Fastmail",
        mcp_url="https://api.fastmail.com/mcp",
        auth_mode=AuthMode.DCR,
        oauth_metadata_url="https://api.fastmail.com/.well-known/oauth-authorization-server",
        scopes=(),  # discovered from metadata
    ),
}
```

Catalog keys share a namespace with `MCPServerRow.name` (which is `unique=True`). A startup validation rejects YAML servers whose `name` collides with any catalog key — clearer than letting the unique constraint fail at insert time.

### Persistence

Two new tables.

**`oauth_credentials`**

| column | type | notes |
|---|---|---|
| `provider_key` | str, PK | matches `OAUTH_CATALOG` key |
| `client_id` | bytes | encrypted (Fernet) |
| `client_secret` | bytes, nullable | encrypted; some DCR registrations are public clients |
| `access_token` | bytes | encrypted |
| `refresh_token` | bytes, nullable | encrypted; some providers omit |
| `token_expires_at` | TZDateTime | absolute, set at exchange time to `now + expires_in` (raw — the refresh-window skew lives in the scheduler) |
| `scopes_granted` | JSON | array of scopes the provider actually granted |
| `status` | str | `connected` \| `needs_reauth` \| `expired_pending_refresh` (transient) |
| `last_error` | text, nullable | populated when `status='needs_reauth'` |
| `connected_at`, `updated_at` | TZDateTime | audit |

**`oauth_pending`** — short-lived rows representing in-progress authorization flows.

| column | type | notes |
|---|---|---|
| `state` | str, PK | random 32-byte url-safe token |
| `provider_key` | str | catalog key |
| `code_verifier` | str | PKCE verifier (held in DB only between Hop 3 and Hop 4 of the flow) |
| `created_at` | TZDateTime | rows older than 10 min are expired |

Migration: a new Alembic revision adds both tables.

## OAuth flow

Four hops. Each is independently unit-testable via `httpx.MockTransport`.

### Hop 1 — Discover

User clicks "Connect Fastmail" on `/mcp` → `GET /oauth/connect/fastmail`. Handler calls `flow.start_authorization("fastmail")`:

1. Load catalog entry. Since `auth_mode=DCR`, `GET` the `oauth_metadata_url` and parse RFC 8414 metadata: `authorization_endpoint`, `token_endpoint`, `registration_endpoint`, `code_challenge_methods_supported`, `revocation_endpoint`.
2. Confirm `S256` is in supported PKCE methods; we don't support `plain`. Otherwise raise.

### Hop 2 — Register (DCR-only, one-time)

If `oauth_credentials` already has a row for `fastmail` with a `client_id`, skip. Otherwise:

1. `POST` to `registration_endpoint` with:
   ```json
   {
     "client_name": "Jarvis",
     "redirect_uris": ["<JARVIS_BASE_URL>/oauth/callback"],
     "grant_types": ["authorization_code", "refresh_token"],
     "token_endpoint_auth_method": "client_secret_basic"
   }
   ```
2. Persist `client_id` (and `client_secret` if returned) into `oauth_credentials`, encrypted via Fernet.

### Hop 3 — Redirect to consent

Generate PKCE: `code_verifier` (random url-safe 64–96 bytes), `code_challenge = base64url(sha256(code_verifier))`. Generate `state` (random 32-byte url-safe).

Build authorization URL:

```
<authorization_endpoint>?
  response_type=code
  &client_id=<client_id>
  &redirect_uri=<redirect_uri>
  &state=<state>
  &code_challenge=<code_challenge>
  &code_challenge_method=S256
  &scope=<space-joined scopes>
```

Insert `oauth_pending` row keyed by `state`. Return 302 to consent URL.

### Hop 4 — Callback

Provider redirects to `GET /oauth/callback?state=…&code=…` (or `?error=...&state=...` on decline).

1. Look up `oauth_pending` by `state`. Missing or older than 10 min → 400 with the `oauth_callback.html` error page; emit `audit:oauth.state_mismatch`.
2. If query has `error=access_denied` (or any `error`), render the "declined" page; sweep the pending row.
3. `POST` to `token_endpoint` with `grant_type=authorization_code`, `code`, `code_verifier`, `redirect_uri`. Authenticate with `client_secret_basic` if a secret exists, else as a public client.
4. Receive `{access_token, refresh_token?, expires_in, token_type, scope?}`. Persist into `oauth_credentials` (encrypted), set `status='connected'`, `token_expires_at = now + expires_in`.
5. Delete the `oauth_pending` row.
6. Call `MCPManager.replace_oauth_server("fastmail", new_headers)` — see Token lifecycle.
7. Render success page linking back to `/mcp`.

### Failure modes covered explicitly

- Metadata fetch fails / returns non-conforming JSON → friendly error on `/mcp`, nothing persisted.
- `registration_endpoint` absent (DCR unsupported) → hard error in v1: "Manual-mode OAuth not yet supported."
- User declines consent (`error=access_denied`) → "you declined" page; sweep pending row.
- `state` mismatch / replay → 400, audit `oauth.state_mismatch`. CSRF defense.
- Network blip during token exchange → no partial persistence; transactional write.

### Audit logging

Every step emits an `audit_events` row, type-prefixed `oauth.*`:

- `oauth.discovery_started`, `oauth.discovery_succeeded`, `oauth.discovery_failed`
- `oauth.dcr_registered`
- `oauth.consent_redirect_issued`
- `oauth.callback_received`, `oauth.state_mismatch`, `oauth.consent_declined`
- `oauth.tokens_obtained`
- `oauth.refresh_succeeded`, `oauth.refresh_transient_failure`, `oauth.refresh_permanently_failed`
- `oauth.revoked`

Tokens themselves never appear in payloads — only `provider_key`, error codes, scope strings.

## Token lifecycle

### Refresh policy: proactive only

A new APScheduler job, `oauth_token_refresh`, runs every 60 seconds. For each provider where `token_expires_at - 90s <= now`, call `flow.refresh(provider_key)`.

- **Success.** Write new tokens; call `MCPManager.replace_oauth_server(provider_key, new_headers)`.
- **Transient failure** (network, 5xx). Exponential backoff, retry up to 3 times across subsequent ticks. Audit `oauth.refresh_transient_failure`.
- **Permanent failure** (`invalid_grant`, 401 from token endpoint, refresh token absent). Set `status='needs_reauth'`, populate `last_error`, drop SDK server via `MCPManager.remove_oauth_server`. Audit `oauth.refresh_permanently_failed`. Surface a banner on `/mcp`.

We are explicitly **not** instrumenting on-401 retry inside agent tool calls. The Agents SDK doesn't give a clean hook, and the 90s skew buffer should make in-call 401s rare. Add later as a `MCPServerStreamableHttp` wrapper if it becomes painful in practice.

### SDK server rebuild mechanics

The current `MCPManager` (`jarvis/mcp/manager.py:24`) uses one shared `AsyncExitStack`. Two structural changes:

1. **Per-server exit stacks.** `self._stack: AsyncExitStack` → `self._stacks: dict[str, AsyncExitStack]`, keyed by server name (catalog key for OAuth servers, YAML name otherwise). `stop()` closes all of them.
2. **`self._sdk_servers: dict[str, object]`** instead of a list. `agent_mcp_servers()` returns `list(self._sdk_servers.values())`. The `AgentRunner` already calls this fresh per run, so each new agent picks up the latest objects.

New methods on `MCPManager`:

- `replace_oauth_server(provider_key, headers)`:
  1. Build new `MCPServerStreamableHttp` with fresh headers on a new exit stack.
  2. Verify `list_tools()` succeeds — catches a botched refresh before cutover.
  3. Atomically: swap `_sdk_servers[provider_key]`, swap `_stacks[provider_key]`, schedule old exit stack to close on next event-loop tick.
  4. Update `mcp_tools` rows for the server (refresh discovered tools).
- `remove_oauth_server(provider_key)`: pop the entry, close the exit stack. Used on refresh-permanent-failure and Disconnect.

In-flight agent calls that hold a reference to the *old* SDK server continue using the old token until their run completes. Since the old token is still valid for ~90 seconds after refresh, this is fine. A tool call that straddles the boundary and fails is the same UX as a transient network error — surface, retry.

### Bootstrap behavior

`MCPManager.start()` connects YAML servers as today, then iterates `OAUTH_CATALOG` and for each provider with `status='connected'` in `oauth_credentials`, builds an SDK server with current tokens. If a token is already expired at boot, run a one-shot inline refresh (don't wait 60s for the scheduler tick). Permanent failure at boot just marks `needs_reauth`; the rest of Jarvis still starts.

### Disconnect path

Dashboard "Disconnect" button → `flow.revoke(provider_key)`:

1. `POST` to provider's `revocation_endpoint` (RFC 7009) if advertised. Best-effort; failures are logged but don't block local cleanup.
2. `MCPManager.remove_oauth_server(provider_key)`.
3. Delete the `oauth_credentials` row entirely (including DCR-registered `client_id`). Next Connect re-registers from scratch — simpler than tracking "client registered but not authorized."

### Pending-row cleanup

Opportunistic: on every `/oauth/callback`, sweep `oauth_pending` rows older than 10 minutes. Plus a daily APScheduler job (`oauth_pending_sweep`) that does the same — covers the case where no callback ever arrives.

## Dashboard UX

The existing `/mcp` page (`jarvis/web/templates/mcp.html`) gets a new "OAuth Providers" section above the existing list, rendered from `OAUTH_CATALOG` joined against `oauth_credentials`.

Per catalog entry, three card states:

- **Disconnected** — display name, "Connect" button (`<a href="/oauth/connect/{key}">`).
- **Connected** — display name, green "Connected" pill, "Disconnect" button, last-refreshed timestamp, and the same tool list rendering used for YAML servers.
- **Needs re-auth** — display name, amber pill, the `last_error` string, and a "Reconnect" button (same target as Connect).

No edit, no add, no client_id input field — DCR handles that invisibly.

**Callback page** at `jarvis/web/templates/oauth_callback.html` renders one of three outcomes: success ("Connected to Fastmail. You can close this tab."), declined, error. Static — no JS auto-close.

**No new top-level nav.** Everything lives under `/mcp`. The mental model stays "MCP servers — some configured in YAML, some authorized via OAuth."

`audit_events` rows with type prefix `oauth.*` automatically appear in the existing `/audit` view; no change needed there.

## Testing

**Unit tests (the bulk).** `flow.start_authorization`, `flow.handle_callback`, `flow.refresh`, `flow.revoke` each take an injected `httpx.AsyncClient` so tests pass a `MockTransport` returning canned responses for metadata, registration, token, and revocation endpoints. Covers happy path plus every failure mode listed above.

**Crypto tests.** Roundtrip Fernet encrypt/decrypt of token blobs. One test pins "key change invalidates existing rows" so we don't ship a quiet data-loss bug if the operator regenerates `JARVIS_SECRETS_KEY`.

**`MCPManager` integration tests.** A `FakeSDKServer` (duck-typed: `__aenter__`, `__aexit__`, `list_tools`) lets us test the new methods without spinning up real MCP. Asserts:

1. Refreshing one OAuth server doesn't close other servers.
2. Failed `list_tools` on the new SDK server aborts the swap; the old server stays active.
3. Bootstrap with already-expired tokens triggers an inline refresh.
4. Per-server exit-stack refactor preserves existing YAML-server behavior — locked in by tests *before* the refactor lands.

**Web route tests.** FastAPI `TestClient` against:

- `/oauth/connect/fastmail` — asserts 302 to consent URL with correct PKCE/state params.
- `/oauth/callback` — asserts state lookup, token-exchange call, redirect to `/mcp`.
- `/mcp` — asserts catalog entries render in the right state for each `oauth_credentials.status`.

**Manual end-to-end against Fastmail.** One required step before merging: run a full Connect → list_tools → wait for refresh → Disconnect cycle against `https://api.fastmail.com/mcp`, paste log output and a `/mcp` screenshot into the PR. This is the only thing that proves DCR actually works against a real provider.

## Open seams (intentional)

- `auth_mode=MANUAL` raises `NotImplementedError` with a message pointing at "future PR." When a real non-DCR HTTP MCP server arrives, manual mode adds a code path in `flow.py` and a UI for pasting client_id/secret on `/mcp`. No schema changes needed; `oauth_credentials` already has `client_id`/`client_secret` columns.
- On-401 retry inside tool calls — extension point is wrapping `MCPServerStreamableHttp`. Not built v1.
- Scope-narrowing UI — would slot into the catalog card on `/mcp` between "Connect" and consent. Not built v1.
