# OAuth Provider Auto-Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Discover" button to the `/mcp` Add Provider form that, given only the MCP server URL, auto-detects the OAuth metadata URL, the auth mode (DCR vs manual), and supported scopes, and pre-fills the form's editable OAuth fields.

**Architecture:** A pure, network-only module `jarvis/oauth/discovery.py` runs the MCP authorization discovery chain (RFC 9728 protected-resource metadata → RFC 8414 / OIDC authorization-server metadata) over an injected `httpx.AsyncClient`, returning a `DiscoveryResult`. A new HTMX route `POST /mcp/providers/discover` renders a Jinja partial that prefills the form fields via out-of-band swaps. No model, migration, or `add_provider` changes.

**Tech Stack:** Python 3.12, httpx (MockTransport for tests), FastAPI, Jinja2, HTMX 2, pytest (`asyncio_mode=auto`).

Spec: `docs/superpowers/specs/2026-06-13-oauth-provider-auto-discovery-design.md`

---

## File Structure

- **Create** `jarvis/oauth/discovery.py` — `DiscoveryResult` dataclass + `discover_provider()` and private helpers. One responsibility: turn an MCP URL into OAuth settings. No DB, no app state.
- **Create** `tests/unit/test_oauth_discovery.py` — MockTransport unit tests, one per chain branch.
- **Modify** `jarvis/web/routes/mcp_admin.py` — add `POST /mcp/providers/discover` route returning an HTMX fragment.
- **Create** `jarvis/web/templates/_provider_discovery.html` — the fragment with OOB-swap inputs.
- **Modify** `jarvis/web/templates/mcp.html:79-82` — add the Discover button, a result `<div>`, and `id` attributes on the three OAuth inputs.
- **Create** `tests/integration/test_web_provider_discovery.py` — route returns a fragment with the discovered metadata URL and auth mode.

---

## Task 1: Discovery module + unit tests

**Files:**
- Create: `jarvis/oauth/discovery.py`
- Test: `tests/unit/test_oauth_discovery.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_oauth_discovery.py`:

```python
"""discover_provider tests using httpx.MockTransport (no real network)."""

import httpx
import pytest

from jarvis.oauth.discovery import discover_provider

AS_META_DCR = {
    "authorization_endpoint": "https://as.example.com/auth",
    "token_endpoint": "https://as.example.com/token",
    "registration_endpoint": "https://as.example.com/register",
    "code_challenge_methods_supported": ["S256"],
    "scopes_supported": ["read", "write"],
}
AS_META_MANUAL = {k: v for k, v in AS_META_DCR.items() if k != "registration_endpoint"}


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_prm_at_path_then_as_metadata_dcr():
    def handler(req):
        p = req.url.path
        if p == "/.well-known/oauth-protected-resource/mcp/v1":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        if req.url.host == "as.example.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META_DCR)
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com/mcp/v1", make_client(handler))
    assert r.oauth_metadata_url == "https://as.example.com/.well-known/oauth-authorization-server"
    assert r.auth_mode == "dcr"
    assert r.scopes_supported == ["read", "write"]


async def test_prm_at_origin_fallback():
    def handler(req):
        p = req.url.path
        if p == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        if req.url.host == "as.example.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META_DCR)
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com/mcp/v1", make_client(handler))
    assert r.auth_mode == "dcr"


async def test_www_authenticate_hint():
    def handler(req):
        p = req.url.path
        if p == "/mcp/v1":
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/prm-doc"'},
            )
        if p == "/prm-doc":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        if req.url.host == "as.example.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META_DCR)
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com/mcp/v1", make_client(handler))
    assert r.oauth_metadata_url == "https://as.example.com/.well-known/oauth-authorization-server"


async def test_as_metadata_at_origin_fastmail_style():
    def handler(req):
        p = req.url.path
        if req.url.host == "api.fastmail.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META_DCR)
        return httpx.Response(404)

    r = await discover_provider("https://api.fastmail.com/mcp", make_client(handler))
    assert r.oauth_metadata_url == "https://api.fastmail.com/.well-known/oauth-authorization-server"
    assert r.auth_mode == "dcr"


async def test_oidc_only_document_manual():
    def handler(req):
        p = req.url.path
        if p == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["https://accounts.example.com"]})
        if req.url.host == "accounts.example.com":
            if p == "/.well-known/oauth-authorization-server":
                return httpx.Response(404)
            if p == "/.well-known/openid-configuration":
                return httpx.Response(200, json=AS_META_MANUAL)
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com/", make_client(handler))
    assert r.oauth_metadata_url == "https://accounts.example.com/.well-known/openid-configuration"
    assert r.auth_mode == "manual"


async def test_multiple_authorization_servers_first_used():
    def handler(req):
        p = req.url.path
        if p == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={"authorization_servers": ["https://as1.example.com", "https://as2.example.com"]},
            )
        if req.url.host == "as1.example.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META_DCR)
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com", make_client(handler))
    assert r.authorization_servers == ["https://as1.example.com", "https://as2.example.com"]
    assert r.oauth_metadata_url == "https://as1.example.com/.well-known/oauth-authorization-server"


async def test_total_miss_returns_notes_no_metadata():
    def handler(req):
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com/mcp", make_client(handler))
    assert r.oauth_metadata_url is None
    assert r.auth_mode is None
    assert r.notes  # non-empty trace


async def test_empty_mcp_url_raises():
    with pytest.raises(ValueError):
        await discover_provider("  ", make_client(lambda req: httpx.Response(404)))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_oauth_discovery.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvis.oauth.discovery'`

- [ ] **Step 3: Write the implementation**

Create `jarvis/oauth/discovery.py`:

```python
"""One-shot OAuth provider discovery for the Add Provider form.

Given only an MCP server URL, derive the authorization server's metadata URL,
whether it supports RFC 7591 DCR, and its advertised scopes — following the MCP
authorization discovery chain (RFC 9728 protected-resource metadata -> RFC 8414 /
OIDC authorization-server metadata). Pure: all HTTP goes through an injected
httpx.AsyncClient, so it is unit-testable with httpx.MockTransport.

Never raises for "couldn't find it" — returns a DiscoveryResult whose
oauth_metadata_url is None and whose `notes` explain what was tried. Raises only
on programmer error (empty mcp_url).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

_PRM_WELL_KNOWN = "/.well-known/oauth-protected-resource"
_AS_WELL_KNOWN = "/.well-known/oauth-authorization-server"
_OIDC_WELL_KNOWN = "/.well-known/openid-configuration"

# Matches resource_metadata="<url>" inside a WWW-Authenticate header.
_RESOURCE_METADATA_RE = re.compile(r'resource_metadata="?([^",\s]+)"?')


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    oauth_metadata_url: str | None = None
    auth_mode: str | None = None  # "dcr" | "manual" | None
    scopes_supported: list[str] = field(default_factory=list)
    authorization_servers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


async def _get_json(http: httpx.AsyncClient, url: str, notes: list[str]) -> dict | None:
    try:
        resp = await http.get(url)
    except httpx.HTTPError as e:
        notes.append(f"GET {url} failed: {e}")
        return None
    if resp.status_code >= 400:
        notes.append(f"GET {url} -> {resp.status_code}")
        return None
    try:
        return resp.json()
    except Exception:
        notes.append(f"GET {url} -> non-JSON body")
        return None


def _servers_from_prm(data: dict | None) -> list[str]:
    if not data:
        return []
    servers = data.get("authorization_servers")
    if isinstance(servers, list) and servers:
        return [str(s).rstrip("/") for s in servers]
    return []


async def _resource_metadata_hint(
    http: httpx.AsyncClient, mcp_url: str, notes: list[str]
) -> str | None:
    try:
        resp = await http.get(mcp_url)
    except httpx.HTTPError as e:
        notes.append(f"Unauthenticated GET {mcp_url} failed: {e}")
        return None
    match = _RESOURCE_METADATA_RE.search(resp.headers.get("WWW-Authenticate", ""))
    if match:
        notes.append("401 WWW-Authenticate advertised resource_metadata.")
        return match.group(1)
    return None


async def _find_authorization_servers(
    http: httpx.AsyncClient, mcp_url: str, notes: list[str]
) -> list[str]:
    origin = _origin(mcp_url)
    path = urlparse(mcp_url).path.rstrip("/")

    # 1 + 2. Protected-resource metadata, path-aware then at the origin.
    prm_candidates = []
    if path:
        prm_candidates.append(f"{origin}{_PRM_WELL_KNOWN}{path}")
    prm_candidates.append(f"{origin}{_PRM_WELL_KNOWN}")
    for prm_url in prm_candidates:
        servers = _servers_from_prm(await _get_json(http, prm_url, notes))
        if servers:
            notes.append(f"Found authorization_servers via PRM at {prm_url}.")
            return servers

    # 3. Unauthenticated request -> WWW-Authenticate resource_metadata hint.
    hint_url = await _resource_metadata_hint(http, mcp_url, notes)
    if hint_url:
        servers = _servers_from_prm(await _get_json(http, hint_url, notes))
        if servers:
            notes.append(f"Found authorization_servers via 401 hint {hint_url}.")
            return servers

    # 4. Assume the authorization server lives at the MCP origin (Fastmail-style).
    notes.append(f"Falling back to authorization-server metadata at origin {origin}.")
    return [origin]


async def _fetch_as_metadata(
    http: httpx.AsyncClient, base: str, notes: list[str]
) -> tuple[str | None, dict | None]:
    base = base.rstrip("/")
    for suffix in (_AS_WELL_KNOWN, _OIDC_WELL_KNOWN):
        url = f"{base}{suffix}"
        data = await _get_json(http, url, notes)
        if data and "authorization_endpoint" in data and "token_endpoint" in data:
            return url, data
    return None, None


async def discover_provider(mcp_url: str, http: httpx.AsyncClient) -> DiscoveryResult:
    mcp_url = mcp_url.strip()
    if not mcp_url:
        raise ValueError("mcp_url is required")
    notes: list[str] = []

    as_bases = await _find_authorization_servers(http, mcp_url, notes)

    for base in as_bases:
        meta_url, meta = await _fetch_as_metadata(http, base, notes)
        if meta is not None:
            auth_mode = "dcr" if meta.get("registration_endpoint") else "manual"
            scopes = [str(s) for s in (meta.get("scopes_supported") or [])]
            notes.append(f"Resolved metadata at {meta_url} (auth_mode={auth_mode}).")
            return DiscoveryResult(
                oauth_metadata_url=meta_url,
                auth_mode=auth_mode,
                scopes_supported=scopes,
                authorization_servers=as_bases,
                notes=notes,
            )

    notes.append("Found authorization server(s) but could not fetch valid metadata.")
    return DiscoveryResult(authorization_servers=as_bases, notes=notes)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_oauth_discovery.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Lint**

Run: `uv run ruff check jarvis/oauth/discovery.py tests/unit/test_oauth_discovery.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add jarvis/oauth/discovery.py tests/unit/test_oauth_discovery.py
git commit -m "feat(oauth): add provider auto-discovery from MCP URL

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Discovery route + fragment template

**Files:**
- Modify: `jarvis/web/routes/mcp_admin.py:1-14` (imports) and add route after `add_provider`
- Create: `jarvis/web/templates/_provider_discovery.html`
- Test: `tests/integration/test_web_provider_discovery.py`

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_web_provider_discovery.py`:

```python
"""POST /mcp/providers/discover returns an HTMX fragment that prefills the form."""

from unittest.mock import MagicMock

import httpx
import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog
from jarvis.oauth.crypto import generate_key
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app

AS_META = {
    "authorization_endpoint": "https://as.example.com/auth",
    "token_endpoint": "https://as.example.com/token",
    "registration_endpoint": "https://as.example.com/register",
    "code_challenge_methods_supported": ["S256"],
    "scopes_supported": ["read"],
}


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    def handler(req):
        p = req.url.path
        if p == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        if req.url.host == "as.example.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META)
        return httpx.Response(404)

    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    ctx.config = MagicMock()
    ctx.config.secrets_key = generate_key().encode()
    ctx.oauth_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_discover_route_returns_fragment_with_metadata(client):
    resp = client.post("/mcp/providers/discover", data={"mcp_url": "https://mcp.example.com"})
    assert resp.status_code == 200
    body = resp.text
    assert "https://as.example.com/.well-known/oauth-authorization-server" in body
    assert "dcr" in body
    # OOB swap targets the form input by id.
    assert 'id="oauth_metadata_url"' in body
    assert 'hx-swap-oob="true"' in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/integration/test_web_provider_discovery.py -q`
Expected: FAIL — 404 (route not registered) so the body assertions fail.

- [ ] **Step 3: Create the fragment template**

Create `jarvis/web/templates/_provider_discovery.html`:

```html
{% if r.oauth_metadata_url %}
<p class="badge-ok">Detected <strong>{{ r.auth_mode }}</strong> &middot; <code>{{ r.oauth_metadata_url }}</code></p>
<input id="oauth_metadata_url" name="oauth_metadata_url" value="{{ r.oauth_metadata_url }}" hx-swap-oob="true">
<select id="auth_mode" name="auth_mode" hx-swap-oob="true">
  <option {% if r.auth_mode == 'dcr' %}selected{% endif %}>dcr</option>
  <option {% if r.auth_mode == 'manual' %}selected{% endif %}>manual</option>
</select>
<input id="default_scopes" name="default_scopes" value="{{ r.scopes_supported | join(' ') }}" hx-swap-oob="true">
{% else %}
<p class="badge-err">Couldn't auto-detect — fill the OAuth fields manually.</p>
{% endif %}
{% if r.notes %}<details><summary>Discovery log</summary><pre>{{ r.notes | join('\n') }}</pre></details>{% endif %}
```

- [ ] **Step 4: Add the route**

In `jarvis/web/routes/mcp_admin.py`, update the FastAPI imports (line 5) to add `HTMLResponse`:

```python
from fastapi.responses import HTMLResponse, RedirectResponse
```

Add the import near the other oauth imports (after line 9, `from jarvis.oauth.catalog import unique_runtime_name`):

```python
from jarvis.oauth.discovery import discover_provider
```

Add this route immediately after the `add_provider` function (after line 73, before `edit_provider_credentials`):

```python
@router.post("/mcp/providers/discover", response_class=HTMLResponse)
async def discover_provider_endpoint(request: Request, mcp_url: str = Form(...)):
    """Probe an MCP URL for OAuth metadata; return an HTMX fragment that prefills
    the Add Provider form. Never fails the request — discovery is best-effort."""
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    result = await discover_provider(mcp_url, ctx.oauth_http)
    return templates.TemplateResponse(request, "_provider_discovery.html", {"r": result})
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/integration/test_web_provider_discovery.py -q`
Expected: PASS (1 passed)

- [ ] **Step 6: Lint**

Run: `uv run ruff check jarvis/web/routes/mcp_admin.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add jarvis/web/routes/mcp_admin.py jarvis/web/templates/_provider_discovery.html tests/integration/test_web_provider_discovery.py
git commit -m "feat(web): add /mcp/providers/discover HTMX route

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Wire the Discover button into the form

**Files:**
- Modify: `jarvis/web/templates/mcp.html:79-82`

- [ ] **Step 1: Edit the Add Provider form**

Replace lines 79-82 of `jarvis/web/templates/mcp.html`:

```html
    <label>MCP URL <input id="mcp_url" name="mcp_url" required placeholder="https://mcp.example.com/mcp"></label>
    <label>Auth mode (oauth) <select name="auth_mode"><option>dcr</option><option>manual</option></select></label>
    <label>OAuth metadata URL (oauth) <input name="oauth_metadata_url"></label>
    <label>Default scopes (space-separated) <input name="default_scopes"></label>
```

with:

```html
    <label>MCP URL <input id="mcp_url" name="mcp_url" required placeholder="https://mcp.example.com/mcp"></label>
    <button type="button" hx-post="/mcp/providers/discover" hx-include="#mcp_url" hx-target="#discovery-result">Discover OAuth settings</button>
    <div id="discovery-result" class="muted"></div>
    <label>Auth mode (oauth) <select id="auth_mode" name="auth_mode"><option>dcr</option><option>manual</option></select></label>
    <label>OAuth metadata URL (oauth) <input id="oauth_metadata_url" name="oauth_metadata_url"></label>
    <label>Default scopes (space-separated) <input id="default_scopes" name="default_scopes"></label>
```

The `type="button"` keeps the Discover button from submitting the form; `hx-include="#mcp_url"` posts just the URL; `hx-target="#discovery-result"` shows the status while the `id`'d inputs are updated by the fragment's out-of-band swaps.

- [ ] **Step 2: Verify the existing e2e flow still passes**

The OOB inputs reuse the same `name` attributes, so `POST /mcp/providers/add` is unchanged.

Run: `uv run pytest tests/integration/test_web_mcp_e2e.py -q`
Expected: PASS (unchanged).

- [ ] **Step 3: Commit**

```bash
git add jarvis/web/templates/mcp.html
git commit -m "feat(web): add Discover button to Add Provider form

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Full verification

- [ ] **Step 1: Run lint + full test suite**

Run: `make check`
Expected: ruff clean, all tests pass (including the new unit + integration tests).

- [ ] **Step 2: Manual smoke (optional, if a dev server is handy)**

Run: `uv run python -m jarvis serve`, open `http://localhost:8080/mcp`, expand "Add provider", enter `https://api.fastmail.com/mcp`, click "Discover OAuth settings". Expect the OAuth metadata URL to fill with `https://api.fastmail.com/.well-known/oauth-authorization-server` and auth mode `dcr`.

- [ ] **Step 3: Final commit (if Step 1 required fixes)**

```bash
git add -A
git commit -m "chore: lint/test fixups for provider auto-discovery

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** discovery module (Task 1) covers the full chain + 4 fallbacks and DCR/manual + scopes detection; route + fragment (Task 2) and form wiring (Task 3) cover the "Discover button prefill" UX; both unit and integration tests present, matching the spec's Testing section. No model/migration changes, matching the spec's Non-Goals.
- **Placeholder scan:** every step has concrete code/commands; no TBDs.
- **Type consistency:** `DiscoveryResult` fields (`oauth_metadata_url`, `auth_mode`, `scopes_supported`, `authorization_servers`, `notes`) are referenced identically in the module, the fragment template (`r.*`), and both tests. `discover_provider(mcp_url, http)` signature is identical across module, route, and tests.
</content>
