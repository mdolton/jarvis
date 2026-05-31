# Gmail OAuth MCP provider (manual-mode OAuth)

**Status:** Approved spec — ready for implementation planning
**Date:** 2026-05-30
**Builds on:** [2026-04-25 OAuth-based MCP server management](./2026-04-25-oauth-mcp-management-design.md)

## Goal

Add Google's official **Gmail MCP server** (`https://gmailmcp.googleapis.com/mcp/v1`, early access) as an OAuth-capable provider in Jarvis. Gmail cannot use the Dynamic Client Registration (DCR) path the existing framework was built around — Google does not advertise a `registration_endpoint` — so this work implements the **manual-mode OAuth** code path that the original spec deliberately stubbed with `NotImplementedError`.

This is **additive**: Gmail is added alongside the existing Fastmail (DCR) entry. Fastmail stays so the DCR path keeps a real catalog entry; users who don't connect it simply see a "Disconnected" card on `/mcp`.

## Confirmed external facts

- **MCP endpoint:** `https://gmailmcp.googleapis.com/mcp/v1`
- **OAuth client:** manually created in Google Cloud Console, type **Web application** (matches the model Google documents for Claude). No DCR.
- **Authorization endpoint:** `https://accounts.google.com/o/oauth2/v2/auth`
- **Token endpoint:** `https://oauth2.googleapis.com/token`
- **Revocation endpoint:** `https://oauth2.googleapis.com/revoke`
- **Discovery:** `https://accounts.google.com/.well-known/openid-configuration` returns all three endpoints and advertises `code_challenge_methods_supported: ["plain", "S256"]`. No `registration_endpoint`.
- **Scopes:** `https://www.googleapis.com/auth/gmail.readonly`, `https://www.googleapis.com/auth/gmail.compose`
- **Redirect URI to register:** `https://jarvis.moltonlava.online/oauth/callback` (i.e. `${JARVIS_BASE_URL}/oauth/callback`). Authorized JavaScript origins: leave empty (server-side code flow, no browser token request).

## Non-goals

- Removing or altering Fastmail / the DCR path.
- A paste-in-UI form for client credentials — credentials come from environment variables.
- Multi-account Gmail. Tokens remain global to the Jarvis instance.
- Per-scope / scope-narrowing UI.
- DB schema changes — `oauth_credentials` already has `client_id`/`client_secret` columns.

## Design

### Architectural choice: reuse `discover()` against Google's well-known doc

`discover()` currently raises `NotImplementedError` for any non-DCR provider. Google's `.well-known/openid-configuration` returns everything `discover()` needs (`authorization_endpoint`, `token_endpoint`, `revocation_endpoint`, `code_challenge_methods_supported` incl. `S256`) and omits only `registration_endpoint`, which `discover()` already reads with `.get()`. So manual mode reuses the existing discovery code unchanged except for removing the guard. Endpoints stay fresh, revocation is auto-discovered, and S256 validation still runs.

Rejected alternatives: hardcoding endpoints in the catalog (drift, loses S256 check) and RFC 9728 protected-resource-metadata discovery off the MCP server (most new code; `/.well-known/oauth-protected-resource` 404'd at the host root — not worth it here).

### 1. Catalog (`jarvis/oauth/catalog.py`)

Add two optional fields to `ProviderEntry` for manual-mode credential sourcing:

```python
client_id_env: str | None = None       # MANUAL mode: env var holding the client_id
client_secret_env: str | None = None   # MANUAL mode: env var holding the client_secret
send_resource_indicator: bool = True    # RFC 8707 toggle (see §4)
```

Add the Gmail entry (Fastmail entry unchanged):

```python
"gmail": ProviderEntry(
    key="gmail",
    display_name="Gmail",
    mcp_url="https://gmailmcp.googleapis.com/mcp/v1",
    auth_mode=AuthMode.MANUAL,
    oauth_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    scopes=(
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ),
    extra_auth_params={"access_type": "offline", "prompt": "consent"},
    client_id_env="GOOGLE_OAUTH_CLIENT_ID",
    client_secret_env="GOOGLE_OAUTH_CLIENT_SECRET",
),
```

`access_type=offline` + `prompt=consent` are **required**: Google only issues a `refresh_token` when `access_type=offline`, and only reliably re-issues one on each authorization when `prompt=consent` is also set. The proactive-refresh scheduler depends on a refresh token; without it the connection dies ~1 hour after Connect and goes `needs_reauth`.

### 2. Flow (`jarvis/oauth/flow.py`)

**`discover()`** — remove the `if entry.auth_mode is not AuthMode.DCR: raise NotImplementedError` guard. No other change; a `None` `registration_endpoint` is already handled.

**`start_authorization()`** — in the get-or-register branch, switch on `auth_mode`:
- `DCR` → `register_client()` as today.
- `MANUAL` → call a new `_resolve_manual_client(entry)` that reads `entry.client_id_env` / `entry.client_secret_env` from `os.environ`. If either is unset (when required), raise `OAuthDiscoveryError` with a friendly, dashboard-renderable message naming the missing variable. Seed the same sentinel credentials row (`access_token_enc=b""`) used by the DCR branch, with the env-sourced `client_id`/`client_secret` encrypted.

No changes needed downstream: PKCE, state insertion, token exchange (`client_secret_basic` when a secret is present — Google web-app clients are confidential), `refresh`, and `revoke` already handle a confidential client correctly.

Credential refresh note: if the operator rotates the Google client credentials, they Disconnect (deletes the row) then Connect (re-seeds from env). Stored credentials win over env once a row exists — documented behavior, not a bug.

### 3. Manager bootstrap (`jarvis/mcp/manager.py`)

`_bootstrap_oauth_catalog()` currently skips non-DCR providers (`if entry.auth_mode is not AuthMode.DCR: continue`), which would leave a connected Gmail unattached at startup. Remove that filter so **any** `status='connected'` provider with an access token re-attaches at boot (DCR and MANUAL alike). The already-expired-at-boot inline refresh path is unchanged.

### 4. RFC 8707 resource indicator (primary verification risk)

The flow sends `resource=entry.mcp_url` on the authorization, token-exchange, and refresh requests. Because Google built `gmailmcp.googleapis.com` specifically as an MCP server, it most likely requires `resource=https://gmailmcp.googleapis.com/mcp/v1`. But Google's general OAuth has historically ignored or rejected unrecognized `resource` values, so:

- Add `send_resource_indicator: bool = True` to `ProviderEntry`; gate the three `params/form["resource"] = entry.mcp_url` sites on it.
- Default `True` for Gmail. If the real Connect cycle returns `invalid_request`/`invalid_target` from Google, flip Gmail's flag to `False` and retry. This is the first thing to confirm in manual E2E.

### 5. Bootstrap wiring (`jarvis/main.py`)

No constructor changes. `OAuthFlow` reads manual credentials from the environment at Connect time via the catalog's env-var field names. (Tests monkeypatch `os.environ`.)

## Persistence

No migration. The existing `oauth_credentials` table already stores encrypted `client_id`/`client_secret`/`access_token`/`refresh_token`. Manual mode populates `client_id`/`client_secret` from env instead of from a DCR response; the schema is identical.

## Testing

**Unit / integration (mock transport, the bulk):**

- `test_oauth_catalog.py` — Gmail entry present with `auth_mode=MANUAL`, correct scopes, `extra_auth_params`, env field names; Fastmail entry still present and DCR.
- `test_oauth_flow.py` (new manual-mode cases):
  - `discover()` succeeds against Google-style metadata that has **no** `registration_endpoint` and advertises `S256`.
  - `start_authorization()` for `gmail` seeds the credentials row from monkeypatched `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET` and builds an auth URL containing `access_type=offline`, `prompt=consent`, the two gmail scopes, `code_challenge_method=S256`, and (default) the `resource` param.
  - Missing env var → `OAuthDiscoveryError` naming the variable; nothing persisted.
  - Token exchange uses `client_secret_basic` (Authorization: Basic …) when the secret is present.
  - `refresh()` and `revoke()` work for the manual confidential client.
  - `send_resource_indicator=False` omits `resource` from all three requests.
  - Existing Fastmail/DCR tests still pass unchanged.
- Manager tests — a `status='connected'` **manual** provider re-attaches at boot; the existing DCR boot-attach test still passes.
- `test_web_oauth.py` — `GET /oauth/connect/gmail` 302s to Google's consent URL with the expected params; `/oauth/callback` completes token exchange and redirects.

**Manual end-to-end (pre-merge gate, replaces the old Fastmail gate):** a full Connect → `list_tools` → wait-for-scheduled-refresh → Disconnect cycle against the real Gmail MCP server, with logs and a `/mcp` screenshot pasted into the PR. This is the only thing that proves manual-mode OAuth + the `resource`-indicator decision actually works against Google. The Fastmail DCR path is no longer manually re-verified; its mock-transport tests stand in.

## Operational setup (docs, `README.md`)

Rewrite/extend the OAuth section to cover Gmail:

1. **Google Cloud Console** (operator has already created the client — these are confirmations):
   - OAuth client type **Web application**.
   - Authorized redirect URI exactly `https://jarvis.moltonlava.online/oauth/callback`. Authorized JavaScript origins: empty.
   - Gmail API enabled; Gmail MCP server early access enabled on the project.
   - OAuth consent screen configured with scopes `gmail.readonly` and `gmail.compose`; while unverified, add `mdolton@gmail.com` as a test user.
2. **Jarvis environment** — set, alongside `JARVIS_BASE_URL` and `JARVIS_SECRETS_KEY`:
   ```
   GOOGLE_OAUTH_CLIENT_ID=<from Google Cloud Console>
   GOOGLE_OAUTH_CLIENT_SECRET=<from Google Cloud Console>
   ```
3. Restart Jarvis, open `/mcp`, click **Connect** on the Gmail card, complete Google consent.

## Open seams (intentional)

- Paste-in-UI credential entry remains a future option; env vars cover the single-operator deployment.
- `send_resource_indicator` is a stopgap toggle; if Google standardizes RFC 9728 protected-resource-metadata for the Gmail MCP server, discovery could later derive the canonical resource value instead of using `mcp_url`.
- On-401 in-call retry is still not built (unchanged from the base spec).
