# MCP Management — Phase 2: Dashboard CRUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Prerequisite:** Phase 1 (`2026-06-13-mcp-management-phase1-model.md`) is merged — `ProviderCatalog`, `MCPProviderRepo`, `MCPConnectionRepo`, `slug_label`, connection-keyed `OAuthFlow`/manager all exist.

**Goal:** Let the operator add/edit/remove providers, add multiple connections per provider (multi-account), enable/disable/remove connections, edit OAuth app credentials and scopes, and enable/disable file-declared stdio servers — all from the `/mcp` tab.

**Architecture:** New mutation endpoints in `jarvis/web/routes/mcp_admin.py` (form POSTs → `303 /mcp`), backed by `MCPProviderRepo` / `MCPConnectionRepo` and the manager's `connect_connection`/`disconnect`. Provider creation supports `oauth` and `http`/`sse` (never stdio). stdio enable/disable is persisted as a `SettingsRepo` override and applied live. Every mutation emits an audit event.

**Tech Stack:** FastAPI forms, Jinja2/HTMX, SQLAlchemy async, Fernet, pytest + `TestClient`.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `jarvis/web/routes/mcp_admin.py` | Provider/connection/stdio mutation endpoints | Create |
| `jarvis/web/app.py` | Router registration | Register `mcp_admin_router` |
| `jarvis/oauth/store.py` | Repo helpers | Add `MCPProviderRepo.has_connections`; connection `runtime_name` uniqueness helper |
| `jarvis/oauth/catalog.py` | slug | Add `unique_runtime_name(existing, provider_key, label)` |
| `jarvis/mcp/manager.py` | stdio toggle support | `start()` honors stdio disabled-override set |
| `jarvis/web/templates/mcp.html` | Full management UI | Replace with forms + per-connection tool tables + stdio toggles |
| `jarvis/web/routes/mcp.py` | Page data | Add stdio `enabled` flag + provider `default_scopes`/`auth_mode` for forms |

**Conventions reused from the codebase:** `ctx = request.app.state.ctx`; form fields via `= Form(...)`; redirect `RedirectResponse(url="/mcp", status_code=303)`; audit via `await ctx.audit.emit(AuditEvent(type=..., payload={...}))`; encryption via `encrypt_blob(plaintext, ctx.config.secrets_key)`.

---

## Task 1: `unique_runtime_name` + `MCPProviderRepo.has_connections`

**Files:**
- Modify: `jarvis/oauth/catalog.py`, `jarvis/oauth/store.py`
- Test: `tests/integration/test_runtime_name_unique.py`, extend `tests/integration/test_provider_catalog.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_runtime_name_unique.py
import pytest_asyncio

from jarvis.oauth.catalog import unique_runtime_name
from jarvis.oauth.store import MCPConnectionRepo, MCPProviderRepo
from jarvis.oauth.catalog import seed_built_in_providers
from jarvis.persistence.db import Base, create_engine, session_factory


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    async with f() as s:
        await seed_built_in_providers(s)
    yield f
    await engine.dispose()


async def test_unique_runtime_name_dedupes(factory):
    async with factory() as s:
        existing = {c.runtime_name for c in await MCPConnectionRepo(s).list_all()}
    assert unique_runtime_name(existing, "calendar", "Work") == "calendar:work"
    existing.add("calendar:work")
    assert unique_runtime_name(existing, "calendar", "Work") == "calendar:work-2"


async def test_has_connections(factory):
    async with factory() as s:
        assert await MCPProviderRepo(s).has_connections("calendar") is False
        await MCPConnectionRepo(s).create(provider_key="calendar", label="W",
                                          runtime_name="calendar:w")
    async with factory() as s:
        assert await MCPProviderRepo(s).has_connections("calendar") is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_runtime_name_unique.py -v`
Expected: FAIL — `ImportError: cannot import name 'unique_runtime_name'`.

- [ ] **Step 3: Implement**

In `catalog.py`:

```python
from collections.abc import Iterable

def unique_runtime_name(existing: Iterable[str], provider_key: str, label: str) -> str:
    base = f"{provider_key}:{slug_label(label) or 'default'}"
    existing = set(existing)
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"
```

In `store.py` `MCPProviderRepo`:

```python
async def has_connections(self, provider_key: str) -> bool:
    from jarvis.persistence.models import MCPConnectionRow
    res = await self._session.execute(
        select(MCPConnectionRow.id).where(MCPConnectionRow.provider_key == provider_key).limit(1)
    )
    return res.first() is not None
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_runtime_name_unique.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/catalog.py jarvis/oauth/store.py tests/integration/test_runtime_name_unique.py
git commit -m "feat(mcp): unique_runtime_name helper + provider.has_connections"
```

---

## Task 2: Connection management endpoints (add / enable / disable / remove)

**Files:**
- Create: `jarvis/web/routes/mcp_admin.py`
- Modify: `jarvis/web/app.py` (register router)
- Test: `tests/integration/test_web_mcp_admin_connections.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_web_mcp_admin_connections.py
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    ctx.config = MagicMock(); ctx.config.secrets_key = __import__("jarvis.oauth.crypto", fromlist=["generate_key"]).generate_key().encode()
    ctx.audit = MagicMock(); ctx.audit.emit = AsyncMock()
    ctx.mcp_manager = MagicMock()
    ctx.mcp_manager.connect_connection = AsyncMock()
    ctx.mcp_manager.disconnect = AsyncMock()
    app = create_app(app_context=ctx)
    c = TestClient(app)
    c._factory = factory
    yield c
    await engine.dispose()


def test_add_connection_creates_row(client):
    resp = client.post("/mcp/connections/add",
                       data={"provider_key": "calendar", "label": "Work"},
                       follow_redirects=False)
    assert resp.status_code == 303
    page = client.get("/mcp").text
    assert "Work" in page


def test_disable_then_enable_connection(client):
    client.post("/mcp/connections/add", data={"provider_key": "gmail", "label": "Personal"},
                follow_redirects=False)
    page = client.get("/mcp").text
    import re
    cid = re.search(r'/mcp/connections/([0-9a-f-]{36})/disable', page).group(1)
    assert client.post(f"/mcp/connections/{cid}/disable", follow_redirects=False).status_code == 303
    client.app.state.ctx.mcp_manager.disconnect.assert_awaited()
    assert client.post(f"/mcp/connections/{cid}/enable", follow_redirects=False).status_code == 303
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_web_mcp_admin_connections.py -v`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Create `mcp_admin.py`**

```python
"""Provider / connection / stdio mutation endpoints for the MCP tab."""
import json
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.oauth.catalog import unique_runtime_name
from jarvis.oauth.crypto import encrypt_blob
from jarvis.oauth.store import MCPConnectionRepo, MCPProviderRepo

router = APIRouter()


def _redirect():
    return RedirectResponse(url="/mcp", status_code=303)


async def _emit(ctx, action: str, **payload):
    emit = getattr(getattr(ctx, "audit", None), "emit", None)
    if emit is not None:
        await emit(AuditEvent(type=AuditEventType.MCP_CONFIG_CHANGED,
                              payload={"action": action, **payload}))


@router.post("/mcp/connections/add")
async def add_connection(
    request: Request,
    provider_key: str = Form(...),
    label: str = Form(...),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    scopes: str = Form(""),
    url_override: str = Form(""),
    headers: str = Form(""),  # newline-separated "Name: value"
):
    ctx = request.app.state.ctx
    entry = await ctx.catalog.get(provider_key)  # raises KeyError -> 500 if unknown; guard below
    async with ctx.session_factory() as session:
        existing = {c.runtime_name for c in await MCPConnectionRepo(session).list_all()}
    rt = unique_runtime_name(existing, provider_key, label)

    key = ctx.config.secrets_key
    cid_enc = encrypt_blob(client_id.encode(), key) if client_id.strip() else None
    sec_enc = encrypt_blob(client_secret.encode(), key) if client_secret.strip() else None
    scope_list = scopes.split() if scopes.strip() else list(entry.default_scopes)
    headers_enc = None
    if entry.kind in ("http", "sse") and headers.strip():
        parsed = {}
        for line in headers.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                parsed[k.strip()] = v.strip()
        headers_enc = encrypt_blob(json.dumps(parsed).encode(), key)

    async with ctx.session_factory() as session:
        conn = await MCPConnectionRepo(session).create(
            provider_key=provider_key, label=label.strip() or "Default", runtime_name=rt,
            client_id_enc=cid_enc, client_secret_enc=sec_enc, scopes=scope_list,
            url_override=url_override.strip() or None, headers_enc=headers_enc)
        conn_id = conn.id
    await _emit(ctx, "connection.add", provider_key=provider_key, runtime_name=rt)

    # http/sse connections are "credentialed and ready" -> attach immediately.
    if entry.kind in ("http", "sse"):
        async with ctx.session_factory() as session:
            conn = await MCPConnectionRepo(session).get(conn_id)
        try:
            await ctx.mcp_manager.connect_connection(conn)
        except Exception:
            pass  # failure is recorded as server status by the manager
    return _redirect()


@router.post("/mcp/connections/{connection_id}/enable")
async def enable_connection(request: Request, connection_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = MCPConnectionRepo(session)
        await repo.set_enabled(connection_id, enabled=True)
        conn = await repo.get(connection_id)
    if conn is None:
        raise HTTPException(404)
    try:
        await ctx.mcp_manager.connect_connection(conn)
    except Exception:
        pass
    await _emit(ctx, "connection.enable", runtime_name=conn.runtime_name)
    return _redirect()


@router.post("/mcp/connections/{connection_id}/disable")
async def disable_connection(request: Request, connection_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = MCPConnectionRepo(session)
        conn = await repo.get(connection_id)
        if conn is None:
            raise HTTPException(404)
        await repo.set_enabled(connection_id, enabled=False)
    await ctx.mcp_manager.disconnect(conn.runtime_name)
    await _emit(ctx, "connection.disable", runtime_name=conn.runtime_name)
    return _redirect()


@router.post("/mcp/connections/{connection_id}/remove")
async def remove_connection(request: Request, connection_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = MCPConnectionRepo(session)
        conn = await repo.get(connection_id)
        if conn is None:
            raise HTTPException(404)
        runtime_name = conn.runtime_name
    await ctx.mcp_manager.disconnect(runtime_name)
    # Best-effort revoke for oauth connections with tokens.
    flow = getattr(ctx, "oauth_flow", None)
    if flow is not None and conn.access_token_enc is not None:
        try:
            await flow.revoke(connection_id)
        except Exception:
            pass
    async with ctx.session_factory() as session:
        await MCPConnectionRepo(session).delete(connection_id)
    await _emit(ctx, "connection.remove", runtime_name=runtime_name)
    return _redirect()
```

Add `MCP_CONFIG_CHANGED` to `AuditEventType` in `jarvis/core/types.py` if absent (mirror an existing member, e.g. `MCP_CONFIG_CHANGED = "mcp_config_changed"`).

- [ ] **Step 4: Register the router in `app.py`**

```python
from jarvis.web.routes.mcp_admin import router as mcp_admin_router
app.include_router(mcp_admin_router)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/integration/test_web_mcp_admin_connections.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/web/routes/mcp_admin.py jarvis/web/app.py jarvis/core/types.py tests/integration/test_web_mcp_admin_connections.py
git commit -m "feat(mcp): connection add/enable/disable/remove endpoints"
```

---

## Task 3: Provider management endpoints (add / edit creds / remove)

**Files:**
- Modify: `jarvis/web/routes/mcp_admin.py`
- Test: `tests/integration/test_web_mcp_admin_providers.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_web_mcp_admin_providers.py
# (reuse the `client` fixture shape from test_web_mcp_admin_connections.py)
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import generate_key
from jarvis.oauth.store import MCPConnectionRepo, MCPProviderRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    ctx.config = MagicMock(); ctx.config.secrets_key = generate_key().encode()
    ctx.audit = MagicMock(); ctx.audit.emit = AsyncMock()
    ctx.mcp_manager = MagicMock(); ctx.mcp_manager.disconnect = AsyncMock()
    app = create_app(app_context=ctx)
    c = TestClient(app); c._factory = factory
    yield c
    await engine.dispose()


def test_add_oauth_provider(client):
    resp = client.post("/mcp/providers/add", data={
        "key": "notion", "display_name": "Notion", "kind": "oauth",
        "mcp_url": "https://mcp.notion.com/mcp", "auth_mode": "dcr",
        "oauth_metadata_url": "https://mcp.notion.com/.well-known/oauth-authorization-server",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "Notion" in client.get("/mcp").text


def test_remove_provider_refused_when_connections_exist(client):
    import asyncio
    async def seed():
        async with client._factory() as s:
            await MCPConnectionRepo(s).create(provider_key="gmail", label="P", runtime_name="gmail:p")
    asyncio.get_event_loop().run_until_complete(seed())
    resp = client.post("/mcp/providers/gmail/remove", follow_redirects=False)
    assert resp.status_code == 400  # refuse: still has connections


def test_builtin_provider_cannot_be_removed(client):
    resp = client.post("/mcp/providers/calendar/remove", follow_redirects=False)
    assert resp.status_code == 400
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_web_mcp_admin_providers.py -v`
Expected: FAIL — 404 (routes absent).

- [ ] **Step 3: Add provider endpoints to `mcp_admin.py`**

```python
@router.post("/mcp/providers/add")
async def add_provider(
    request: Request,
    key: str = Form(...),
    display_name: str = Form(...),
    kind: str = Form(...),            # 'oauth' | 'http' | 'sse'
    mcp_url: str = Form(...),
    auth_mode: str = Form("dcr"),
    oauth_metadata_url: str = Form(""),
    default_scopes: str = Form(""),
    header_names: str = Form(""),
):
    ctx = request.app.state.ctx
    if kind not in ("oauth", "http", "sse"):
        raise HTTPException(400, "kind must be oauth, http, or sse (stdio is file-managed)")
    key = key.strip()
    if not key:
        raise HTTPException(400, "key required")
    async with ctx.session_factory() as session:
        repo = MCPProviderRepo(session)
        if await repo.get(key) is not None:
            raise HTTPException(400, f"provider {key!r} already exists")
        await repo.upsert(
            key=key, display_name=display_name.strip(), kind=kind, mcp_url=mcp_url.strip(),
            builtin=False,
            auth_mode=(auth_mode if kind == "oauth" else None),
            oauth_metadata_url=(oauth_metadata_url.strip() or None) if kind == "oauth" else None,
            pkce=True, send_resource_indicator=True, extra_auth_params={},
            default_scopes=default_scopes.split() if default_scopes.strip() else [],
            header_names=[h.strip() for h in header_names.split(",") if h.strip()],
        )
    await _emit(ctx, "provider.add", provider_key=key, kind=kind)
    return _redirect()


@router.post("/mcp/providers/{provider_key}/edit-credentials")
async def edit_provider_credentials(
    request: Request, provider_key: str,
    apply_to: str = Form("all"),  # which connections get the new app creds: 'all'
    client_id: str = Form(...),
    client_secret: str = Form(""),
):
    """Set OAuth app credentials on this provider's connections (creds live on connections)."""
    ctx = request.app.state.ctx
    key = ctx.config.secrets_key
    cid_enc = encrypt_blob(client_id.encode(), key)
    sec_enc = encrypt_blob(client_secret.encode(), key) if client_secret.strip() else None
    async with ctx.session_factory() as session:
        crepo = MCPConnectionRepo(session)
        conns = await crepo.list_for_provider(provider_key)
        for c in conns:
            await crepo.set_client(c.id, client_id_enc=cid_enc, client_secret_enc=sec_enc)
    await _emit(ctx, "provider.edit_credentials", provider_key=provider_key, count=len(conns))
    return _redirect()


@router.post("/mcp/providers/{provider_key}/remove")
async def remove_provider(request: Request, provider_key: str):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = MCPProviderRepo(session)
        prov = await repo.get(provider_key)
        if prov is None:
            raise HTTPException(404)
        if prov.builtin:
            raise HTTPException(400, "built-in providers cannot be removed")
        if await repo.has_connections(provider_key):
            raise HTTPException(400, "remove its connections first")
        await repo.delete(provider_key)
    await _emit(ctx, "provider.remove", provider_key=provider_key)
    return _redirect()
```

> Design note: app credentials live on the connection, so "edit provider credentials" fans the new client_id/secret out to that provider's connections. New connections created afterward take creds from their own add form (prefilled in the UI from an existing connection if present — Task 5).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_web_mcp_admin_providers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/web/routes/mcp_admin.py tests/integration/test_web_mcp_admin_providers.py
git commit -m "feat(mcp): provider add/edit-credentials/remove endpoints (refuse builtin + in-use removal)"
```

---

## Task 4: stdio enable/disable override

**Files:**
- Modify: `jarvis/mcp/manager.py` (honor disabled set in `start()`), `jarvis/web/routes/mcp_admin.py` (toggle endpoint), `jarvis/web/routes/mcp.py` (expose `enabled`)
- Test: `tests/integration/test_stdio_toggle.py`

Override stored under `SettingsRepo` key `"mcp.stdio_disabled"` = JSON list of disabled stdio names.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_stdio_toggle.py
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo, SettingsRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)
        await MCPServerRepo(s).upsert(name="fs", transport="stdio")
    ctx = MagicMock(); ctx.session_factory = factory; ctx.catalog = ProviderCatalog(factory)
    ctx.audit = MagicMock(); ctx.audit.emit = AsyncMock()
    ctx.mcp_manager = MagicMock(); ctx.mcp_manager.disconnect = AsyncMock()
    app = create_app(app_context=ctx)
    c = TestClient(app); c._factory = factory
    yield c
    await engine.dispose()


def test_disable_stdio_persists_override(client):
    assert client.post("/mcp/stdio/fs/disable", follow_redirects=False).status_code == 303
    client.app.state.ctx.mcp_manager.disconnect.assert_awaited_with("fs")
    import asyncio
    async def read():
        async with client._factory() as s:
            return await SettingsRepo(s).get("mcp.stdio_disabled")
    assert "fs" in (asyncio.get_event_loop().run_until_complete(read()) or [])
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_stdio_toggle.py -v`
Expected: FAIL — 404.

- [ ] **Step 3: Add the toggle endpoints + manager gate**

In `mcp_admin.py`:

```python
from jarvis.persistence.repositories import SettingsRepo

_STDIO_DISABLED_KEY = "mcp.stdio_disabled"

async def _set_stdio_disabled(ctx, name: str, disabled: bool) -> None:
    async with ctx.session_factory() as session:
        repo = SettingsRepo(session)
        current = set(await repo.get(_STDIO_DISABLED_KEY) or [])
        if disabled:
            current.add(name)
        else:
            current.discard(name)
        await repo.set(_STDIO_DISABLED_KEY, sorted(current))


@router.post("/mcp/stdio/{name}/disable")
async def disable_stdio(request: Request, name: str):
    ctx = request.app.state.ctx
    await _set_stdio_disabled(ctx, name, True)
    await ctx.mcp_manager.disconnect(name)
    await _emit(ctx, "stdio.disable", name=name)
    return _redirect()


@router.post("/mcp/stdio/{name}/enable")
async def enable_stdio(request: Request, name: str):
    ctx = request.app.state.ctx
    await _set_stdio_disabled(ctx, name, False)
    cfg = next((s for s in ctx.config.mcp_servers.servers if s.name == name), None)
    if cfg is not None:
        await ctx.mcp_manager.connect_server(cfg)  # thin wrapper over connect_cfg (Phase 1 Task 7)
    await _emit(ctx, "stdio.enable", name=name)
    return _redirect()
```

Add a `connect_server` wrapper to `MCPManager` (mirrors `connect_connection` but for a `MCPServerConfig`):

```python
async def connect_server(self, cfg) -> None:
    await self._submit("connect_cfg", cfg)
```

In `MCPManager.start()`, gate the stdio connect loop on the disabled override:

```python
async with self._session_factory() as session:
    from jarvis.persistence.repositories import SettingsRepo
    disabled = set(await SettingsRepo(session).get("mcp.stdio_disabled") or [])
for server_cfg in self._config.servers:
    if not server_cfg.enabled or server_cfg.name in disabled:
        continue
    await self._submit("connect_cfg", server_cfg)
```

- [ ] **Step 4: Expose `enabled` for stdio rows in `routes/mcp.py`**

When building `stdio_servers`, attach an `enabled` flag:

```python
async with ctx.session_factory() as session:
    disabled = set(await SettingsRepo(session).get("mcp.stdio_disabled") or [])
stdio_servers = [{"name": s.name, "status": s.status, "last_error": s.last_error,
                  "enabled": s.name not in disabled, "tools": server_tools.get(s.id, [])}
                 for s in servers if s.source == "stdio"]
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/integration/test_stdio_toggle.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/web/routes/mcp_admin.py jarvis/web/routes/mcp.py jarvis/mcp/manager.py tests/integration/test_stdio_toggle.py
git commit -m "feat(mcp): stdio enable/disable override (persisted, applied live)"
```

---

## Task 5: Full management template

**Files:**
- Modify: `jarvis/web/templates/mcp.html`
- Modify: `jarvis/web/routes/mcp.py` (pass `default_scopes`, `auth_mode` for forms)
- Test: `tests/integration/test_web_mcp_template.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_web_mcp_template.py
# (reuse the admin client fixture; seed gmail provider + one connection + a stdio server)
def test_page_renders_forms_and_controls(admin_client):
    page = admin_client.get("/mcp").text
    assert 'action="/mcp/providers/add"' in page
    assert 'action="/mcp/connections/add"' in page
    assert "Add connection" in page
```

(Define `admin_client` like the Task 2 fixture, additionally seeding one connection and one stdio server.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_web_mcp_template.py -v`
Expected: FAIL — forms not present.

- [ ] **Step 3: Replace `mcp.html`**

Render: per-provider block with its connections (each showing runtime status badge, Connect/Disconnect for oauth, Enable/Disable, Remove, and the tool table from `c.tools`), an "Add connection" form per provider (prefilled scopes from `p.default_scopes`; client_id/secret + headers fields shown by `p.kind`), an "Add provider" form, and a stdio section with Enable/Disable toggles. Keep the existing tool-policy form markup (`/mcp/tools/{id}/policy`) inside each connection's tool table so Phase 1's policy feature keeps working. Full template:

```html
{% extends "base.html" %}
{% block title %}MCP Servers{% endblock %}
{% block content %}
<section class="page-head"><div><h1>MCP</h1>
  <p class="muted">Providers, account connections, stdio servers, tools, and policy.</p></div></section>

<section class="section-block">
  <h2>Providers &amp; connections</h2>
  {% for p in providers %}
  <div class="provider-row">
    <h3>{{ p.display_name }} <span class="badge">{{ p.kind }}</span>
      {% if p.builtin %}<span class="badge">built-in</span>{% endif %}</h3>
    {% for c in p.connections %}
    <div class="conn-row">
      <strong>{{ c.label }}</strong>
      <span class="badge {% if c.runtime_status=='connected' %}badge-ok{% elif c.runtime_status=='error' %}badge-err{% else %}badge-warn{% endif %}">{{ c.runtime_status }}</span>
      {% if p.kind == 'oauth' %}
        {% if not c.authorized or c.auth_status == 'needs_reauth' %}
          <a class="btn" href="/oauth/connect/{{ c.id }}">Connect</a>
        {% else %}
          <form method="post" action="/oauth/disconnect/{{ c.id }}" class="inline-form"><button>Disconnect</button></form>
        {% endif %}
      {% endif %}
      {% if c.enabled %}
        <form method="post" action="/mcp/connections/{{ c.id }}/disable" class="inline-form"><button>Disable</button></form>
      {% else %}
        <form method="post" action="/mcp/connections/{{ c.id }}/enable" class="inline-form"><button>Enable</button></form>
      {% endif %}
      <form method="post" action="/mcp/connections/{{ c.id }}/remove" class="inline-form"><button>Remove</button></form>
      {% if c.last_error %}<pre>{{ c.last_error }}</pre>{% endif %}
      {% if c.tools %}
      <table class="ops-table">
        <thead><tr><th>Tool</th><th>Description</th><th>Read-only</th><th>Destructive</th><th>Policy</th></tr></thead>
        <tbody>
        {% for t in c.tools %}
          <tr><td><code>{{ t.name }}</code></td><td>{{ t.description or '—' }}</td>
          <td>{{ '✓' if t.read_only_hint else '—' }}</td><td>{{ '⚠' if t.destructive_hint else '—' }}</td>
          <td><form method="post" action="/mcp/tools/{{ t.id }}/policy" data-policy-tool="{{ t.name }}" class="inline-form policy-form">
            <select name="policy_override">
              <option value="" {% if not t.policy_override %}selected{% endif %}>auto-detect</option>
              <option value="allow" {% if t.policy_override=='allow' %}selected{% endif %}>allow</option>
              <option value="confirm" {% if t.policy_override=='confirm' %}selected{% endif %}>confirm</option>
              <option value="deny" {% if t.policy_override=='deny' %}selected{% endif %}>deny</option>
            </select><button>Save</button></form></td></tr>
        {% endfor %}
        </tbody></table>
      {% endif %}
    </div>
    {% endfor %}

    <details class="add-form"><summary>Add connection</summary>
    <form method="post" action="/mcp/connections/add" class="stacked-form">
      <input type="hidden" name="provider_key" value="{{ p.key }}">
      <label>Label <input name="label" required placeholder="Personal"></label>
      {% if p.kind == 'oauth' %}
        {% if p.auth_mode == 'manual' %}
          <label>Client ID <input name="client_id"></label>
          <label>Client secret <input name="client_secret" type="password"></label>
        {% endif %}
        <label>Scopes (space-separated) <input name="scopes" value="{{ p.default_scopes|join(' ') }}"></label>
      {% else %}
        <label>URL override <input name="url_override" placeholder="{{ p.mcp_url }}"></label>
        <label>Headers (one per line, "Name: value")<textarea name="headers"></textarea></label>
      {% endif %}
      <button type="submit">Add connection</button>
    </form></details>

    {% if not p.builtin %}
    <form method="post" action="/mcp/providers/{{ p.key }}/remove" class="inline-form"><button>Remove provider</button></form>
    {% endif %}
  </div>
  {% endfor %}

  <details class="add-form"><summary>Add provider</summary>
  <form method="post" action="/mcp/providers/add" class="stacked-form">
    <label>Key <input name="key" required placeholder="notion"></label>
    <label>Display name <input name="display_name" required></label>
    <label>Kind <select name="kind"><option>oauth</option><option>http</option><option>sse</option></select></label>
    <label>MCP URL <input name="mcp_url" required placeholder="https://mcp.example.com/mcp"></label>
    <label>Auth mode (oauth) <select name="auth_mode"><option>dcr</option><option>manual</option></select></label>
    <label>OAuth metadata URL (oauth) <input name="oauth_metadata_url"></label>
    <label>Default scopes (space-separated) <input name="default_scopes"></label>
    <button type="submit">Add provider</button>
  </form></details>
</section>

<section class="section-block">
  <h2>stdio servers <span class="muted">(file-managed)</span></h2>
  {% for srv in stdio_servers %}
  <div class="server-block">
    <h3>{{ srv.name }} <span class="badge {% if srv.status=='connected' %}badge-ok{% elif srv.status=='error' %}badge-err{% else %}badge-warn{% endif %}">{{ srv.status }}</span></h3>
    {% if srv.enabled %}
      <form method="post" action="/mcp/stdio/{{ srv.name }}/disable" class="inline-form"><button>Disable</button></form>
    {% else %}
      <form method="post" action="/mcp/stdio/{{ srv.name }}/enable" class="inline-form"><button>Enable</button></form>
    {% endif %}
    {% if srv.last_error %}<pre>{{ srv.last_error }}</pre>{% endif %}
  </div>
  {% endfor %}
  {% if not stdio_servers %}<p class="muted">No stdio servers configured.</p>{% endif %}
</section>
{% endblock %}
```

- [ ] **Step 4: Add `default_scopes`/`auth_mode`/`mcp_url` to provider views in `routes/mcp.py`**

```python
provider_views = [{
    "key": p.key, "display_name": p.display_name, "kind": p.kind, "builtin": p.builtin,
    "auth_mode": p.auth_mode, "mcp_url": p.mcp_url, "default_scopes": p.default_scopes or [],
    "connections": conns_by_provider.get(p.key, []),
} for p in providers]
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/integration/test_web_mcp_template.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/web/templates/mcp.html jarvis/web/routes/mcp.py tests/integration/test_web_mcp_template.py
git commit -m "feat(mcp): full provider/connection/stdio management UI"
```

---

## Task 6: End-to-end add-OAuth-provider → connection → connect happy path

**Files:**
- Test: `tests/integration/test_web_mcp_e2e.py`

- [ ] **Step 1: Write the test**

```python
# tests/integration/test_web_mcp_e2e.py
"""Add a DCR oauth provider, add a connection, kick off connect (start_authorization)."""
import httpx
import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import generate_key
from jarvis.oauth.flow import OAuthFlow
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app

META = {"authorization_endpoint": "https://ex.com/auth", "token_endpoint": "https://ex.com/token",
        "registration_endpoint": "https://ex.com/register", "code_challenge_methods_supported": ["S256"]}


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)

    def handler(request):
        if request.url.path.endswith(".well-known/oauth-authorization-server"):
            return httpx.Response(200, json=META)
        if request.url.path == "/register":
            return httpx.Response(201, json={"client_id": "dcr-cid"})
        return httpx.Response(404)

    from unittest.mock import AsyncMock, MagicMock
    ctx = MagicMock(); ctx.session_factory = factory; ctx.catalog = ProviderCatalog(factory)
    ctx.config = MagicMock(); ctx.config.secrets_key = generate_key().encode()
    ctx.audit = MagicMock(); ctx.audit.emit = AsyncMock()
    ctx.mcp_manager = MagicMock(); ctx.mcp_manager.connect_connection = AsyncMock()
    ctx.oauth_flow = OAuthFlow(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                              session_factory=factory, base_url="http://localhost:8080",
                              secrets_key=ctx.config.secrets_key, catalog=ctx.catalog)
    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_add_provider_connection_then_connect_redirects_to_consent(client):
    client.post("/mcp/providers/add", data={
        "key": "ex", "display_name": "Example", "kind": "oauth",
        "mcp_url": "https://ex.com/mcp", "auth_mode": "dcr",
        "oauth_metadata_url": "https://ex.com/.well-known/oauth-authorization-server"},
        follow_redirects=False)
    client.post("/mcp/connections/add", data={"provider_key": "ex", "label": "Mine"},
                follow_redirects=False)
    page = client.get("/mcp").text
    import re
    cid = re.search(r'/oauth/connect/([0-9a-f-]{36})', page).group(1)
    resp = client.get(f"/oauth/connect/{cid}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://ex.com/auth")
```

- [ ] **Step 2: Run to verify it passes (after wiring)**

Run: `uv run pytest tests/integration/test_web_mcp_e2e.py -v`
Expected: PASS. If `GET /oauth/connect/{connection_id}` (Phase 1 Task 10) does not yet accept a UUID path, fix that handler to `connection_id: UUID` and call `start_authorization(connection_id)`.

- [ ] **Step 3: Full suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_web_mcp_e2e.py
git commit -m "test(mcp): e2e add provider -> connection -> connect"
```

---

## Phase 2 self-review checklist

- [ ] Add provider (oauth/http/sse), reject stdio kind → 400.
- [ ] Add ≥2 connections to one provider; each gets a distinct `runtime_name`.
- [ ] Disable connection → `manager.disconnect(runtime_name)` called; row `enabled=False`; survives restart (not in `list_enabled`).
- [ ] Remove connection → disconnect + revoke (if oauth+token) + delete.
- [ ] Remove provider refused when builtin or has connections (400).
- [ ] stdio Disable persists in `SettingsRepo` and `manager.start()` skips it next boot.
- [ ] Per-connection tool policy form still works (Phase 1 feature intact).
- [ ] `uv run pytest -q` green.
