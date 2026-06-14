# OAuth Provider Auto-Discovery for the Add Provider Form

**Date:** 2026-06-13
**Status:** Approved (design)

## Problem

When adding an OAuth-based MCP server through the `/mcp` dashboard, the operator
must supply two things that provider documentation rarely exposes:

- **OAuth metadata URL** — the authorization server's RFC 8414 / OIDC discovery
  document (e.g. `https://api.fastmail.com/.well-known/oauth-authorization-server`).
- **Auth mode** — whether the provider supports RFC 7591 Dynamic Client
  Registration (`dcr`) or requires a manually-registered `client_id`/`client_secret`
  (`manual`).

Most docs only give you the MCP server URL. The MCP authorization spec, however,
defines a discovery chain that derives both facts from that single URL. This
feature adds a **"Discover" button** to the Add Provider form that runs that chain
and pre-fills the OAuth fields, which the operator then reviews and submits.

## Goals

- From just the MCP server URL, auto-detect the OAuth metadata URL, the auth mode
  (DCR vs manual), and the supported scopes.
- Keep the operator in control: discovery *prefills* editable fields; it does not
  save anything on its own.
- Be robust to real-world providers that don't cleanly expose RFC 9728
  protected-resource metadata (e.g. Fastmail, where the authorization server lives
  at the MCP origin).
- Additive and low-risk: no model, migration, or `add_provider` route changes.

## Non-Goals

- No changes to the runtime OAuth state machine (`flow.py`) — discovery is a
  one-shot setup helper, not part of authorize/refresh.
- No auto-submit. Discovery never writes a provider row; the existing
  `POST /mcp/providers/add` flow is unchanged.
- No new persistence. Results are transient, used only to populate form fields.

## Architecture

A new pure module **`jarvis/oauth/discovery.py`**, separate from `flow.py`. All
HTTP goes through an injected `httpx.AsyncClient` (the shared `ctx.oauth_http`),
so it is unit-testable with `httpx.MockTransport` exactly like `flow.py`.

### Public API

```python
async def discover_provider(mcp_url: str, http: httpx.AsyncClient) -> DiscoveryResult
```

```python
@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    oauth_metadata_url: str | None      # None when discovery failed
    auth_mode: str | None               # "dcr" | "manual" | None
    scopes_supported: list[str]         # for the default_scopes field
    authorization_servers: list[str]    # populated when >1 AS is advertised
    notes: list[str]                    # human-readable trace of what was tried
```

`discover_provider` **never raises** for "couldn't find it." On a total miss it
returns `oauth_metadata_url=None`, `auth_mode=None`, and a populated `notes` trace.
It only raises on programmer error (e.g. empty `mcp_url`).

### Discovery chain (full + fallbacks, in order)

Each step appends to `notes`. The first step that yields a usable authorization
server base wins; the chain then fetches that AS's metadata.

1. **PRM at the MCP path** — `GET {mcp_url}/.well-known/oauth-protected-resource`
   and the path-aware variant. Read `authorization_servers[0]`.
2. **PRM at the origin** — the same well-known at the bare origin.
3. **401 hint** — unauthenticated `GET {mcp_url}`, parse
   `WWW-Authenticate: Bearer resource_metadata="…"`, fetch that document.
4. **AS metadata directly at the origin** — try
   `/.well-known/oauth-authorization-server` then `/.well-known/openid-configuration`
   at the MCP origin (covers Fastmail-style servers where the AS is the origin).

### Resolving the authorization-server metadata

Once an AS base URL is found, fetch its RFC 8414 metadata, falling back to
`/.well-known/openid-configuration`. From the metadata document:

- **DCR support** = `registration_endpoint` is present → `auth_mode = "dcr"`,
  otherwise `auth_mode = "manual"`.
- `scopes_supported` (if present) becomes the prefilled `default_scopes`.
- The metadata document URL that succeeded becomes `oauth_metadata_url`.

This mirrors how `OAuthFlow.discover()` already parses metadata and how
`register_client()` already treats a missing `registration_endpoint` as
"DCR unsupported."

## UI Wiring

### New route

`POST /mcp/providers/discover` in `jarvis/web/routes/mcp_admin.py`:

- Takes `mcp_url: str = Form(...)`.
- Calls `discover_provider(mcp_url, ctx.oauth_http)`.
- Renders a small Jinja partial and returns it as an HTMX fragment.
- Catches nothing exotic — discovery returns a result object either way, so HTMX
  always receives a fragment.

### Template changes (`jarvis/web/templates/mcp.html`)

- A **"Discover" button** beside the MCP URL field in the Add Provider form:
  `hx-post="/mcp/providers/discover"`, includes the `mcp_url` input, targets
  `#discovery-result`.
- A `<div id="discovery-result">` region inside the form.
- The returned fragment populates the existing `oauth_metadata_url`, `auth_mode`,
  and `default_scopes` inputs via out-of-band swaps (`hx-swap-oob`) or a tiny
  inline script setting `.value`. The fields stay **visible and editable** —
  discovery prefills, the operator confirms, then submits the normal
  `/mcp/providers/add` form, which is unchanged.
- On failure, the fragment renders the `notes` trace plus a message such as
  "Couldn't auto-detect — fill the OAuth fields manually." The form still submits.
- When `authorization_servers` has more than one entry, the fragment surfaces the
  list in the notes so the operator knows a choice was made (first one used).

### No changes to

- `MCPProviderRow` / models / migrations.
- `add_provider` route — discovery only populates fields the form already posts.

## Error Handling

- `discover_provider` is total: any network/parse failure becomes a `notes` entry,
  not an exception.
- Individual HTTP probes are wrapped so one failed step does not abort the chain.
- The route always returns a renderable fragment; the form is always submittable
  with manual values as a fallback.

## Testing

### Unit (`tests/unit/`)

Drive `discover_provider` with `httpx.MockTransport`, one test per branch:

- PRM-at-path success → metadata URL + auth mode.
- PRM-at-origin fallback (path PRM 404s).
- 401 `WWW-Authenticate` hint path.
- AS-at-origin / Fastmail shape (no PRM at all).
- OIDC-only document (`openid-configuration`, no `oauth-authorization-server`).
- `registration_endpoint` present → `auth_mode == "dcr"`.
- `registration_endpoint` absent → `auth_mode == "manual"`.
- `scopes_supported` flows into `scopes_supported`.
- Multiple `authorization_servers` → list populated, first used.
- Total miss → `oauth_metadata_url is None` with non-empty `notes`.

No network access in any test.

### Integration (`tests/integration/`)

`POST /mcp/providers/discover` against a mounted mock authorization server returns
a fragment containing the discovered metadata URL and the `dcr` auth mode.

## Risks / Open Questions

- Some providers advertise large `scopes_supported` lists (e.g. Google). Prefill
  them as-is; the operator trims before submitting. (No filtering in v1.)
- Providers behind early-access allowlists (e.g. Gmail MCP) may 401 or 403 the
  unauthenticated probe; that degrades to a `notes` entry and manual entry, which
  is acceptable.
</content>
