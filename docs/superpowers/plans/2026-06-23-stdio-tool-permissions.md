# stdio MCP Tool Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose per-tool policy controls and a per-server "Allow all tools" action for stdio MCP servers in the `/mcp` dashboard, so operators can proactively grant standing permission instead of approving every call in the actions tab.

**Architecture:** stdio tools are already enumerated and persisted to `mcp_tools` with a `policy_override` column that already drives runtime approval; the only gaps are a bulk-update repo method, a bulk route, and the dashboard template (which never rendered the policy table for stdio servers). No schema change; no reconnect needed for a policy to take effect (the route clears the policy cache).

**Tech Stack:** Python 3.12, FastAPI + Jinja2 (HTMX dashboard), SQLAlchemy async (SQLite), pytest (`asyncio_mode=auto`), `uv` for all commands.

---

## File structure

- **Modify** `jarvis/persistence/repositories.py` — add `MCPToolRepo.set_policy_override_for_server` (bulk `UPDATE`).
- **Modify** `jarvis/web/routes/mcp_admin.py` — add `POST /mcp/stdio/{name}/tools/allow-all`; widen imports to `MCPServerRepo`, `MCPToolRepo`.
- **Modify** `jarvis/web/templates/mcp.html` — extract the policy table into a `tools_table` macro, reuse it in the connections section, render it + an "Allow all tools" button in the stdio section.
- **Modify** `tests/integration/test_mcp_approval_policy.py` — add the repo bulk-update test and the per-tool runtime-flip regression test (the file already has the `factory` fixture and a `_tool` helper).
- **Create** `tests/integration/test_web_mcp_stdio_tools.py` — web tests for the allow-all route and the template rendering.

Per-tool policy persistence already works via the existing `POST /mcp/tools/{tool_id}/policy` route (`jarvis/web/routes/mcp.py:78`), which is keyed by `tool_id` and source-agnostic — no change needed there.

---

## Task 1: Bulk policy-override repo method

**Files:**
- Modify: `jarvis/persistence/repositories.py` (class `MCPToolRepo`, after `set_policy_override` at ~line 1015–1021)
- Test: `tests/integration/test_mcp_approval_policy.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_mcp_approval_policy.py` (uses the existing `factory` fixture and `MCPToolDescriptor` import already at the top):

```python
async def test_set_policy_override_for_server_bulk(factory):
    async with factory() as session:
        server = await MCPServerRepo(session).upsert(name="brave", transport="stdio")
        await MCPToolRepo(session).replace_for_server(
            server.id,
            tools=[
                MCPToolDescriptor(name="brave_web_search", input_schema={}),
                MCPToolDescriptor(name="brave_local_search", input_schema={}),
            ],
        )

    async with factory() as session:
        await MCPToolRepo(session).set_policy_override_for_server(server.id, "allow")

    async with factory() as session:
        tools = await MCPToolRepo(session).list_for_server(server.id)
    assert {t.name: t.policy_override for t in tools} == {
        "brave_web_search": "allow",
        "brave_local_search": "allow",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_mcp_approval_policy.py::test_set_policy_override_for_server_bulk -q`
Expected: FAIL with `AttributeError: 'MCPToolRepo' object has no attribute 'set_policy_override_for_server'`

- [ ] **Step 3: Write minimal implementation**

In `jarvis/persistence/repositories.py`, add this method to `MCPToolRepo` immediately after `set_policy_override` (the `update` import and `MCPToolRow` are already imported and used by `set_policy_override`):

```python
    async def set_policy_override_for_server(
        self, server_id: UUID, policy_override: str | None
    ) -> None:
        """Bulk-set policy_override for every tool of a server."""
        await self._session.execute(
            update(MCPToolRow)
            .where(MCPToolRow.server_id == server_id)
            .values(policy_override=policy_override)
        )
        await self._session.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_mcp_approval_policy.py::test_set_policy_override_for_server_bulk -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis/persistence/repositories.py tests/integration/test_mcp_approval_policy.py
git commit -m "feat: bulk set_policy_override_for_server on MCPToolRepo"
```

---

## Task 2: Per-tool runtime-flip regression test

This task adds no production code — it pins the runtime behaviour this feature relies on: setting a stdio tool's `policy_override` to `allow` (what the existing per-tool route does) flips `MCPApprovalPolicy.needs_approval` from `True` to `False` after the policy cache is cleared. `MCPApprovalPolicy` caches tools per server, so the test must call `clear_server` after mutating the DB, exactly as the route does via `clear_policy_cache`.

**Files:**
- Test: `tests/integration/test_mcp_approval_policy.py`

- [ ] **Step 1: Write the test**

Add to `tests/integration/test_mcp_approval_policy.py`:

```python
async def test_allow_override_flips_needs_approval_for_stdio_tool(factory):
    async with factory() as session:
        server = await MCPServerRepo(session).upsert(name="brave", transport="stdio")
        await MCPToolRepo(session).replace_for_server(
            server.id,
            tools=[MCPToolDescriptor(name="brave_web_search", input_schema={})],
        )
    tool_id = (
        await _only_tool_id(factory, server.id)
    )

    policy = MCPApprovalPolicy(session_factory=factory)
    # Non-read-prefixed, no hints -> defaults to CONFIRM.
    assert await policy.needs_approval("brave", _tool("brave_web_search")) is True

    async with factory() as session:
        await MCPToolRepo(session).set_policy_override(tool_id, "allow")
    policy.clear_server("brave")

    assert await policy.needs_approval("brave", _tool("brave_web_search")) is False


async def _only_tool_id(factory, server_id):
    async with factory() as session:
        tools = await MCPToolRepo(session).list_for_server(server_id)
    assert len(tools) == 1
    return tools[0].id
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_mcp_approval_policy.py::test_allow_override_flips_needs_approval_for_stdio_tool -q`
Expected: PASS (this validates existing production behaviour)

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_mcp_approval_policy.py
git commit -m "test: pin stdio tool allow-override runtime flip"
```

---

## Task 3: Bulk "allow all" route

**Files:**
- Modify: `jarvis/web/routes/mcp_admin.py` (imports at line 13; new handler after `enable_stdio` at ~line 257)
- Create: `tests/integration/test_web_mcp_stdio_tools.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_web_mcp_stdio_tools.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import generate_key
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client_and_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)
        server = await MCPServerRepo(s).upsert(name="brave", transport="stdio")
        await MCPToolRepo(s).replace_for_server(
            server.id,
            tools=[
                MCPToolDescriptor(name="brave_web_search", input_schema={}),
                MCPToolDescriptor(name="brave_local_search", input_schema={}),
            ],
        )
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    ctx.config = MagicMock()
    ctx.config.secrets_key = generate_key().encode()
    ctx.audit = MagicMock()
    ctx.audit.emit = AsyncMock()
    ctx.mcp_manager = MagicMock()
    app = create_app(app_context=ctx)
    yield TestClient(app), factory, ctx
    await engine.dispose()


def test_allow_all_sets_every_tool_and_clears_cache(client_and_factory):
    client, factory, ctx = client_and_factory

    resp = client.post("/mcp/stdio/brave/tools/allow-all", follow_redirects=False)
    assert resp.status_code == 303

    import asyncio

    async def overrides():
        async with factory() as s:
            servers = await MCPServerRepo(s).list_all()
            server = next(x for x in servers if x.name == "brave")
            tools = await MCPToolRepo(s).list_for_server(server.id)
        return {t.name: t.policy_override for t in tools}

    assert asyncio.get_event_loop().run_until_complete(overrides()) == {
        "brave_web_search": "allow",
        "brave_local_search": "allow",
    }
    ctx.mcp_manager.clear_policy_cache.assert_called_once_with("brave")
    ctx.audit.emit.assert_awaited()


def test_allow_all_unknown_server_404s(client_and_factory):
    client, _factory, _ctx = client_and_factory
    resp = client.post("/mcp/stdio/nope/tools/allow-all", follow_redirects=False)
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_web_mcp_stdio_tools.py -q`
Expected: FAIL — `test_allow_all_sets_every_tool_and_clears_cache` returns 404 (route not registered) instead of 303.

- [ ] **Step 3: Widen imports**

In `jarvis/web/routes/mcp_admin.py`, change the persistence import (line 13) from:

```python
from jarvis.persistence.repositories import SettingsRepo
```

to:

```python
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo, SettingsRepo
```

- [ ] **Step 4: Add the route**

In `jarvis/web/routes/mcp_admin.py`, add after `enable_stdio` (end of file, ~line 257):

```python
@router.post("/mcp/stdio/{name}/tools/allow-all")
async def allow_all_stdio_tools(request: Request, name: str):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        servers = await MCPServerRepo(session).list_all()
        row = next(
            (s for s in servers if s.name == name and s.source == "stdio"), None
        )
        if row is None:
            raise HTTPException(404, "stdio server not found")
        await MCPToolRepo(session).set_policy_override_for_server(row.id, "allow")
    mcp_manager = getattr(ctx, "mcp_manager", None)
    clear_policy_cache = getattr(mcp_manager, "clear_policy_cache", None)
    if callable(clear_policy_cache):
        clear_policy_cache(name)
    await _emit(ctx, "stdio.tools.allow_all", name=name)
    return _redirect()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_web_mcp_stdio_tools.py -q`
Expected: PASS (both tests)

- [ ] **Step 6: Commit**

```bash
git add jarvis/web/routes/mcp_admin.py tests/integration/test_web_mcp_stdio_tools.py
git commit -m "feat: POST /mcp/stdio/{name}/tools/allow-all bulk grant"
```

---

## Task 4: Render the policy table for stdio servers (template)

**Files:**
- Modify: `jarvis/web/templates/mcp.html`
- Test: `tests/integration/test_web_mcp_stdio_tools.py` (add a render test)

- [ ] **Step 1: Write the failing render test**

Add to `tests/integration/test_web_mcp_stdio_tools.py`:

```python
def test_stdio_section_renders_tools_table_and_allow_all(client_and_factory):
    client, _factory, _ctx = client_and_factory
    page = client.get("/mcp").text
    # per-tool policy form for a stdio tool
    assert "brave_web_search" in page
    assert 'data-policy-tool="brave_web_search"' in page
    # bulk allow-all button for the server
    assert 'action="/mcp/stdio/brave/tools/allow-all"' in page
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_web_mcp_stdio_tools.py::test_stdio_section_renders_tools_table_and_allow_all -q`
Expected: FAIL — `'data-policy-tool="brave_web_search"' in page` is False (stdio section renders no tools table yet).

- [ ] **Step 3: Add the macro and reuse it**

In `jarvis/web/templates/mcp.html`, insert the macro definition right after the `{% block title %}` line (line 2), before `{% block content %}`:

```jinja
{% macro tools_table(tools) %}
{% if tools %}
<table class="ops-table">
  <thead><tr><th>Tool</th><th>Description</th><th>Read-only</th><th>Destructive</th><th>Policy</th></tr></thead>
  <tbody>
  {% for t in tools %}
    <tr><td><code>{{ t.name }}</code></td><td>{{ t.description or '—' }}</td>
    <td>{{ '✓' if t.read_only_hint else '—' }}</td><td>{{ '⚠' if t.destructive_hint else '—' }}</td>
    <td><form method="post" action="/mcp/tools/{{ t.id }}/policy" data-policy-tool="{{ t.name }}" class="inline-form policy-form">
      <select name="policy_override">
        <option value="" {% if not t.policy_override %}selected{% endif %}>auto-detect</option>
        <option value="allow" {% if t.policy_override=='allow' %}selected{% endif %}>allow</option>
        <option value="confirm" {% if t.policy_override=='confirm' %}selected{% endif %}>confirm</option>
        <option value="deny" {% if t.policy_override=='deny' %}selected{% endif %}>deny</option>
      </select><button type="submit">Save</button></form></td></tr>
  {% endfor %}
  </tbody></table>
{% endif %}
{% endmacro %}
```

- [ ] **Step 4: Replace the inline connections table with the macro**

In `jarvis/web/templates/mcp.html`, replace the connections-section block (currently lines 31–47, the `{% if c.tools %} … </table> {% endif %}`):

```jinja
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
            </select><button type="submit">Save</button></form></td></tr>
        {% endfor %}
        </tbody></table>
      {% endif %}
```

with:

```jinja
      {{ tools_table(c.tools) }}
```

- [ ] **Step 5: Render the table + allow-all button in the stdio section**

In `jarvis/web/templates/mcp.html`, in the stdio `server-block` (currently lines 92–100), replace:

```jinja
    {% if srv.last_error %}<pre>{{ srv.last_error }}</pre>{% endif %}
  </div>
```

with:

```jinja
    {% if srv.last_error %}<pre>{{ srv.last_error }}</pre>{% endif %}
    {{ tools_table(srv.tools) }}
    {% if srv.tools %}
    <form method="post" action="/mcp/stdio/{{ srv.name }}/tools/allow-all" class="inline-form">
      <button type="submit">Allow all tools</button>
    </form>
    {% endif %}
  </div>
```

- [ ] **Step 6: Run the render test + the existing template regression suite**

Run: `uv run pytest tests/integration/test_web_mcp_stdio_tools.py tests/integration/test_web_mcp_template.py -q`
Expected: PASS (new render test passes; existing template tests, including the connections table and `/mcp/stdio/fs/disable` toggle, still pass — confirming the macro refactor preserved the connections section).

- [ ] **Step 7: Commit**

```bash
git add jarvis/web/templates/mcp.html tests/integration/test_web_mcp_stdio_tools.py
git commit -m "feat: render stdio tool policy table + Allow all button"
```

---

## Task 5: Full verification

- [ ] **Step 1: Run lint + the full test suite**

Run: `make check`
Expected: ruff clean; all tests pass.

- [ ] **Step 2: Manual smoke (optional, requires a running stack)**

With a stdio server like `brave-search` configured, start the app (`uv run python -m jarvis serve`), open `/mcp`, confirm the stdio server now shows its tools with policy dropdowns and an "Allow all tools" button. Set `brave_web_search` to `allow` (or click "Allow all tools"), then ask the agent to use it — it should run without an actions-tab prompt.

---

## Self-review notes

- **Spec coverage:** repo bulk method (Task 1), per-tool runtime flip / spec test 1 (Task 2), bulk route + spec test 2 (Task 3), template render + spec test 3 (Task 4). All spec sections covered.
- **No schema change**, consistent with the spec's "Data/runtime — no change".
- **Type/name consistency:** `set_policy_override_for_server(server_id, policy_override)` defined in Task 1 is called identically in Task 3; the `tools_table` macro defined in Task 4 Step 3 is invoked in Steps 4–5; route path `/mcp/stdio/{name}/tools/allow-all` matches across route, template, and tests.
- **YAGNI:** no deny-all/reset, no connection-side bulk, no config keys — matches spec scope.
