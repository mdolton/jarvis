# Low-Severity Fixes + PR #52 Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the eleven small fixes from the spec: OAuth flow robustness (malformed refresh response, resource indicator vs url_override, cache eviction on revoke), route fixes (DCR error page, edit-credentials 400), the vanished-connection callback test, MCP manager hygiene (collision-correct prompt wire names, annotation truth), the preference-batch dedupe footgun, tracer docstring rot, and the `AppContext.shutdown()` aiosqlite connection leak.

**Architecture:** Point fixes at existing seams — no new modules. The only diagnostic task is Task 6 (shutdown leak), which carries its own reproduction recipe and acceptance criteria.

**Tech Stack:** Python 3.12, httpx MockTransport, SQLAlchemy async + aiosqlite, FastAPI TestClient, pytest (`asyncio_mode = auto`).

Spec: `docs/superpowers/specs/2026-07-08-low-severity-fixes-design.md`

## Global Constraints

- Use `uv run` for every command; there is no activated venv.
- Pytest `asyncio_mode = auto`: `async def test_*`, NO `@pytest.mark.asyncio`. `pytest.ini` already passes `-q` via addopts — run bare `uv run pytest` when you need the final "N passed" summary line (an extra `-q` suppresses it).
- DB access only through repository classes.
- Ruff line length 100. Run `uv run ruff check jarvis tests` before each commit.
- Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Exact values from the spec: malformed refresh responses raise `OAuthRefreshTransientError` (never Permanent); the edit-credentials rejection is HTTP 400 with detail `"provider has no connections — add a connection first, then set credentials"`; the resource indicator value is `conn.url_override or entry.mcp_url` at all three sites.

---

### Task 1: OAuth flow robustness — malformed refresh response, resource indicator, cache eviction on revoke

**Files:**
- Modify: `jarvis/oauth/flow.py`
- Test: `tests/integration/test_oauth_flow.py` (append)

**Interfaces:**
- Consumes: existing `_refresh_counting_handler(fastmail_metadata_payload, refresh_calls)` helper, `db_factory` / `fastmail_metadata_payload` fixtures, `make_client`, `_make_connection` in the test file.
- Produces: `refresh()` raises `OAuthRefreshTransientError` on a 200 with a non-JSON body or missing `access_token`; `resource` params carry `conn.url_override or entry.mcp_url`; `revoke()` evicts `_last_refresh`/`_refresh_locks` for the connection.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_oauth_flow.py`. Add `unquote_plus` to the existing `urllib.parse` import, and add `OAuthRefreshTransientError` to the `jarvis.oauth.flow` import if not already imported at module level (it may be imported locally inside older tests — module level is fine).

```python
async def test_refresh_malformed_response_raises_transient(db_factory, fastmail_metadata_payload):
    state = {"mode": "authcode"}

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            if state["mode"] == "authcode":
                state["mode"] = "not-json"
                return httpx.Response(
                    200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}
                )
            if state["mode"] == "not-json":
                state["mode"] = "no-token"
                return httpx.Response(200, text="<html>gateway error page</html>")
            return httpx.Response(200, json={"token_type": "Bearer"})
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory),
                     refresh_coalesce_window_sec=0.0)
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    st = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=st, code="abc")

    from jarvis.oauth.flow import OAuthRefreshTransientError

    with pytest.raises(OAuthRefreshTransientError, match="not JSON"):
        await flow.refresh(conn.id)
    with pytest.raises(OAuthRefreshTransientError, match="missing access_token"):
        await flow.refresh(conn.id)

    # Transient means NOT needs_reauth.
    async with db_factory() as session:
        stored = await MCPConnectionRepo(session).get(conn.id)
        assert stored.status == "connected"


async def test_resource_indicator_uses_url_override(db_factory, fastmail_metadata_payload):
    seen = {"token_resources": []}

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            form = dict(p.split("=", 1) for p in request.read().decode().split("&"))
            seen["token_resources"].append(unquote_plus(form.get("resource", "")))
            return httpx.Response(
                200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}
            )
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory),
                     refresh_coalesce_window_sec=0.0)
    async with db_factory() as s:
        conn = await MCPConnectionRepo(s).create(
            provider_key="fastmail", label="alt", runtime_name="fastmail:alt",
            url_override="https://alt.example/mcp",
        )

    consent_url = await flow.start_authorization(conn.id)
    qs = parse_qs(urlparse(consent_url).query)
    assert qs["resource"] == ["https://alt.example/mcp"]

    await flow.handle_callback(state=qs["state"][0], code="abc")
    await flow.refresh(conn.id)
    # Code exchange + refresh exchange both carried the override.
    assert seen["token_resources"] == ["https://alt.example/mcp", "https://alt.example/mcp"]


async def test_revoke_evicts_refresh_cache(db_factory, fastmail_metadata_payload):
    refresh_calls = {"count": 0}
    handler = _refresh_counting_handler(fastmail_metadata_payload, refresh_calls)
    key = generate_key().encode()
    # Default 30s coalesce window: only eviction can allow a second exchange.
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    st = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=st, code="abc")

    await flow.refresh(conn.id)
    assert refresh_calls["count"] == 1

    # revoke's HTTP call 404s against this handler — best-effort, never raises.
    await flow.revoke(conn.id)

    await flow.refresh(conn.id)
    assert refresh_calls["count"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_oauth_flow.py -q`
Expected: `test_refresh_malformed_response_raises_transient` FAILS (JSON decode error / `KeyError: 'access_token'` instead of `OAuthRefreshTransientError`); `test_resource_indicator_uses_url_override` FAILS (`qs["resource"] == ["https://api.fastmail.com/mcp"]`); `test_revoke_evicts_refresh_cache` FAILS (`refresh_calls["count"] == 1` after the second refresh — served from cache).

- [ ] **Step 3: Implement in `jarvis/oauth/flow.py`**

3a. In `_refresh_locked`, replace:

```python
        data = resp.json()
        access_token: str = data["access_token"]
```

with:

```python
        try:
            data = resp.json()
        except Exception as exc:
            raise OAuthRefreshTransientError(
                "token endpoint returned malformed response: not JSON"
            ) from exc
        access_token = data.get("access_token")
        if not access_token:
            raise OAuthRefreshTransientError(
                "token endpoint returned malformed response: missing access_token"
            )
```

3b. The three `resource` sites. In `start_authorization`:

```python
        # RFC 8707 + MCP authorization spec: identify the protected resource.
        if entry.send_resource_indicator:
            params["resource"] = entry.mcp_url
```

becomes `params["resource"] = conn.url_override or entry.mcp_url`. In `handle_callback` and `_refresh_locked`, the analogous `form["resource"] = entry.mcp_url` lines become `form["resource"] = conn.url_override or entry.mcp_url`. (All three methods already have `conn` in scope.)

3c. In `revoke`, directly after the two `RuntimeError` guard clauses, insert:

```python
        # Evict this connection's refresh-coalescing state: after revoke the
        # cached headers hold a dead token. An in-flight refresh racing this
        # pop is benign — worst case one extra token exchange on a fresh lock.
        self._last_refresh.pop(connection_id, None)
        self._refresh_locks.pop(connection_id, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_oauth_flow.py tests/integration/test_oauth_flow_connections.py tests/integration/test_oauth_jobs.py tests/integration/test_web_oauth.py -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow.py
git commit -m "fix: refresh response validation, resource indicator override, cache eviction on revoke

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Route fixes — DCR error page + edit-credentials 400

**Files:**
- Modify: `jarvis/web/routes/oauth.py` (`oauth_connect` except clause + import)
- Modify: `jarvis/web/routes/mcp_admin.py` (`edit_provider_credentials`)
- Test: `tests/integration/test_web_oauth.py` (append), `tests/integration/test_web_mcp_admin_providers.py` (append)

**Interfaces:**
- Consumes: `DCRUnsupportedError` from `jarvis.oauth.flow` (raised by `register_client` when the provider advertises no `registration_endpoint`).
- Produces: GET `/oauth/connect/{id}` renders the error template (502) for DCR-unsupported providers; POST `/mcp/providers/{key}/edit-credentials` returns 400 for connection-less providers, before any encryption.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_web_oauth.py` (uses the file's existing `factory`, `make_flow`, `make_ctx`, `make_app`, `_make_connection`, `fastmail_metadata` helpers):

```python
async def test_connect_dcr_unsupported_renders_error_page(factory):
    # Metadata WITHOUT registration_endpoint: DCR registration is impossible.
    meta = {k: v for k, v in fastmail_metadata().items() if k != "registration_endpoint"}

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=meta)
        return httpx.Response(404)

    flow = make_flow(factory, handler)
    ctx = make_ctx(factory, flow)
    conn = await _make_connection(factory, provider_key="fastmail", runtime_name="fastmail:default")

    client = make_app(ctx)
    r = client.get(f"/oauth/connect/{conn.id}", follow_redirects=False)
    assert r.status_code == 502
    assert "Authorization failed" in r.text
    assert "does not support DCR" in r.text
```

Append to `tests/integration/test_web_mcp_admin_providers.py` (uses the file's existing `client` fixture; `fastmail` is seeded with zero connections):

```python
def test_edit_credentials_400_when_no_connections(client):
    resp = client.post(
        "/mcp/providers/fastmail/edit-credentials",
        data={"client_id": "cid", "client_secret": "sec"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "no connections" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_web_oauth.py::test_connect_dcr_unsupported_renders_error_page tests/integration/test_web_mcp_admin_providers.py::test_edit_credentials_400_when_no_connections -q`
Expected: the DCR test FAILS — `client.get` raises `DCRUnsupportedError` out of the TestClient (Starlette's TestClient re-raises unhandled server exceptions by default), proving the route doesn't catch it; the credentials test FAILS with a 303 redirect (silent no-op).

- [ ] **Step 3: Implement**

3a. `jarvis/web/routes/oauth.py` — extend the flow import:

```python
from jarvis.oauth.flow import DCRUnsupportedError, OAuthCallbackError, OAuthDiscoveryError
```

and in `oauth_connect` change `except OAuthDiscoveryError as e:` to:

```python
    except (OAuthDiscoveryError, DCRUnsupportedError) as e:
```

3b. `jarvis/web/routes/mcp_admin.py` — replace the body of `edit_provider_credentials` so the emptiness check precedes any encryption:

```python
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        crepo = MCPConnectionRepo(session)
        conns = await crepo.list_for_provider(provider_key)
        if not conns:
            raise HTTPException(
                400,
                "provider has no connections — add a connection first, then set credentials",
            )
        key = ctx.config.secrets_key
        cid_enc = encrypt_blob(client_id.encode(), key)
        sec_enc = encrypt_blob(client_secret.encode(), key) if client_secret.strip() else None
        for c in conns:
            await crepo.set_client(c.id, client_id_enc=cid_enc, client_secret_enc=sec_enc)
    await _emit(ctx, "provider.edit_credentials", provider_key=provider_key, count=len(conns))
    return _redirect()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_web_oauth.py tests/integration/test_web_mcp_admin_providers.py -q`
Expected: all pass (including the pre-existing `test_edit_credentials_updates_all_connections`, which creates connections first).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/web/routes/oauth.py jarvis/web/routes/mcp_admin.py tests/integration/test_web_oauth.py tests/integration/test_web_mcp_admin_providers.py
git commit -m "fix: friendly error page for DCR-unsupported; 400 on credential edit with no connections

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Test for the vanished-connection callback branch

**Files:**
- Test: `tests/integration/test_web_oauth.py` (append; test-only task)

**Interfaces:**
- Consumes: the `conn is None` branch in `oauth_callback` (renders error 500, message "connection was removed before the MCP attach could run"), plus the file's helpers.
- Produces: coverage for that branch.

- [ ] **Step 1: Write the test (it should PASS immediately — the branch already exists; this is coverage, not TDD-red)**

```python
async def test_callback_vanished_connection_renders_error(factory, monkeypatch):
    """The branch where the connection row disappears between token exchange and
    attach: monkeypatch the ROUTE MODULE's repo so its re-fetch returns None.
    handle_callback uses its own import and is unaffected."""

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}
            )
        return httpx.Response(404)

    flow = make_flow(factory, handler)
    ctx = make_ctx(factory, flow)
    ctx.mcp_manager = _ManagerStub()
    conn = await _make_connection(factory, provider_key="fastmail", runtime_name="fastmail:default")

    client = make_app(ctx)
    r = client.get(f"/oauth/connect/{conn.id}", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]

    # Patch AFTER the connect request (oauth_connect uses the same module repo).
    from jarvis.web.routes import oauth as oauth_routes

    class _NoneRepo:
        def __init__(self, session):
            pass

        async def get(self, connection_id):
            return None

    monkeypatch.setattr(oauth_routes, "MCPConnectionRepo", _NoneRepo)

    r2 = client.get(f"/oauth/callback?state={state}&code=abc")
    assert r2.status_code == 500
    assert "removed before the MCP attach" in r2.text
    assert ctx.mcp_manager.connected == []  # no attach was attempted
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/integration/test_web_oauth.py::test_callback_vanished_connection_renders_error -q`
Expected: PASS. If it fails, the branch regressed — investigate before proceeding.

- [ ] **Step 3: Run the file and commit**

```bash
uv run pytest tests/integration/test_web_oauth.py -q
uv run ruff check jarvis tests
git add tests/integration/test_web_oauth.py
git commit -m "test: cover vanished-connection callback branch

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Manager hygiene — collision-correct prompt wire names + annotation truth

**Files:**
- Modify: `jarvis/mcp/manager.py`
- Test: `tests/integration/test_mcp_manager_connections.py` (append)

**Interfaces:**
- Consumes: existing `_tool_wire_names(namespace, raw_tool_names) -> dict[str, str]` (raw → wire, collision-digested).
- Produces: `self._wire_names_by_server: dict[str, dict[str, str]]` maintained alongside the other per-server dicts; `agent_mcp_context()` reads it; `_do_replace_oauth` annotated `-> object`.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_mcp_manager_connections.py` (the file has `factory`, `_FakeSDK`, `patch`, `Tool`, `MCPManager`, `MCPServersConfig`, `ProviderCatalog`, `MCPConnectionRepo`, `generate_key`; `MCPProviderRepo` is imported locally inside an existing test — do the same):

```python
async def test_agent_mcp_context_uses_collision_digested_wire_names(factory):
    key = generate_key().encode()
    from jarvis.oauth.store import MCPProviderRepo

    async with factory() as s:
        await MCPProviderRepo(s).upsert(
            key="internal", display_name="Internal", kind="http",
            mcp_url="http://svc.local/mcp", builtin=False, auth_mode=None,
            oauth_metadata_url=None, pkce=True, send_resource_indicator=True,
            extra_auth_params={}, default_scopes=[], header_names=[],
        )
    async with factory() as s:
        await MCPConnectionRepo(s).create(
            provider_key="internal", label="Prod", runtime_name="internal:prod",
        )

    class _CollidingSDK(_FakeSDK):
        async def list_tools(self):
            # Both sanitize to "do_thing": the second must get a digest suffix.
            return [
                Tool(name="do thing", inputSchema={}),
                Tool(name="do-thing", inputSchema={}),
            ]

    mgr = MCPManager(
        config=MCPServersConfig(servers=[]),
        session_factory=factory,
        secrets_key=key,
        oauth_flow=None,
        catalog=ProviderCatalog(factory),
    )
    with patch("jarvis.mcp.manager._build_streamable_http", return_value=_CollidingSDK()):
        await mgr.start()
    try:
        from jarvis.mcp.manager import _tool_wire_names

        expected = _tool_wire_names("prod.internal", ["do thing", "do-thing"])
        assert expected["do thing"] != expected["do-thing"]  # sanity: they collided
        context = mgr.agent_mcp_context()
        assert expected["do thing"] in context
        assert expected["do-thing"] in context
    finally:
        await mgr.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_mcp_manager_connections.py -q`
Expected: the new test FAILS — the context recomputes `_tool_wire_name` per tool (no digest), so the digested wire name is absent.

- [ ] **Step 3: Implement in `jarvis/mcp/manager.py`**

3a. In `__init__`, next to `self._tool_namespaces_by_server: dict[str, str] = {}`, add:

```python
        # Collision-digested raw->wire tool-name map per server, captured at
        # discovery time so agent_mcp_context advertises names that exist.
        self._wire_names_by_server: dict[str, dict[str, str]] = {}
```

3b. In `_do_connect_one`, directly after `self._tool_names_by_server[cfg.name] = tuple(t.name for t in tools)`:

```python
            self._wire_names_by_server[cfg.name] = _tool_wire_names(
                self._tool_namespaces_by_server[cfg.name], [t.name for t in tools]
            )
```

3c. In `_do_replace_oauth`, directly after `self._tool_names_by_server[provider_key] = tuple(t.name for t in tools)`:

```python
        self._wire_names_by_server[provider_key] = _tool_wire_names(
            tool_namespace, [t.name for t in tools]
        )
```

3d. In `_do_remove_oauth`, alongside the other pops: `self._wire_names_by_server.pop(provider_key, None)`. In `_do_stop_all`, alongside the other clears: `self._wire_names_by_server.clear()`.

3e. In `agent_mcp_context`, replace:

```python
            if tools:
                exposed = ", ".join(
                    f"{_tool_wire_name(namespace, tool)} (raw: {tool})" for tool in tools
                )
```

with:

```python
            if tools:
                wire_by_raw = self._wire_names_by_server.get(name, {})
                exposed = ", ".join(
                    f"{wire_by_raw.get(tool, _tool_wire_name(namespace, tool))} (raw: {tool})"
                    for tool in tools
                )
```

3f. Change `_do_replace_oauth`'s signature annotation from `-> None` to `-> object` and append to its docstring: `Returns the new SDK server object; refresh_oauth_server_for_retry consumes it through _submit's result.` Append to `replace_oauth_server`'s docstring: `The underlying command returns the new SDK server (used by the refresh-retry path); this wrapper discards it.`

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_mcp_manager_connections.py tests/integration/test_mcp_manager.py tests/integration/test_mcp_manager_oauth.py tests/integration/test_mcp_manager_lifecycle.py -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/mcp/manager.py tests/integration/test_mcp_manager_connections.py
git commit -m "fix: agent_mcp_context uses collision-digested wire names; annotation truth

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Preference-batch dedupe + tracer docstring

**Files:**
- Modify: `jarvis/persistence/repositories.py` (`MemoryPreferenceRepo.create_pending_many`, `_create_missing_pending`)
- Modify: `jarvis/audit/tracer.py` (module docstring only)
- Test: `tests/integration/test_preference_batch_dedupe.py` (new file)

**Interfaces:**
- Consumes: existing `NewPreference`, `_normalize_preference_content`.
- Produces: `create_pending_many` deduplicates its batch by normalized content (first occurrence wins) before inserting; same for `_create_missing_pending`.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_preference_batch_dedupe.py`:

```python
"""A batch with internally-duplicate normalized content persists one row, not zero."""

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MemoryPreferenceRepo, NewPreference


async def test_create_pending_many_with_internal_duplicate_persists_one(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    try:
        async with factory() as s:
            rows = await MemoryPreferenceRepo(s).create_pending_many(
                items=[
                    NewPreference(content="Prefer concise answers."),
                    NewPreference(content="prefer  concise answers."),  # same normalized
                ],
                source="agent_proposal",
            )
        assert len(rows) == 1
        assert rows[0].content == "Prefer concise answers."  # first occurrence wins

        async with factory() as s:
            stored = await MemoryPreferenceRepo(s).list_for_dedup()
        assert len(stored) == 1
    finally:
        await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_preference_batch_dedupe.py -q`
Expected: FAIL — today the internal duplicate raises `IntegrityError`, the retry re-collides, and `create_pending_many` returns `[]` (assert `len(rows) == 1` fails with 0).

- [ ] **Step 3: Implement in `jarvis/persistence/repositories.py`**

3a. Add a module-level helper next to `_normalize_preference_content`:

```python
def _dedupe_new_preferences(items: list[NewPreference]) -> list[NewPreference]:
    """Drop items whose normalized content repeats within the batch (first wins)."""
    deduped: list[NewPreference] = []
    seen: set[str] = set()
    for item in items:
        normalized = _normalize_preference_content(item.content)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(item)
    return deduped
```

3b. In `create_pending_many`, after the `if not items: return []` guard, add `items = _dedupe_new_preferences(items)` (the rest of the method operates on the deduped list, including the `_create_missing_pending` retry call).

3c. In `_create_missing_pending`, change the `missing` computation to dedupe as well:

```python
        missing = _dedupe_new_preferences(
            [
                item
                for item in items
                if _normalize_preference_content(item.content) not in existing
            ]
        )
```

- [ ] **Step 4: Fix the tracer docstring in `jarvis/audit/tracer.py`**

Replace the docstring paragraph:

```
We install this via `set_trace_processors([JarvisTraceProcessor(...)])` in
the bootstrap, which replaces the default OpenAI-backend exporter. When
paired with `set_tracing_disabled(True)` in llm_client.install_as_default,
no traces leak to OpenAI.
```

with:

```
We install this via `set_trace_processors([JarvisTraceProcessor(...)])` in
the bootstrap, which REPLACES the SDK's default processor list (including
the OpenAI-backend exporter) — that alone keeps traces local.
`install_as_default` deliberately does NOT call `set_tracing_disabled(True)`,
because that would silence this processor too.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_preference_batch_dedupe.py tests/integration/test_repositories_memory.py tests/integration/test_memory_service_summarize.py tests/integration/test_tracer.py -q`
Expected: all pass.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/persistence/repositories.py jarvis/audit/tracer.py tests/integration/test_preference_batch_dedupe.py
git commit -m "fix: preference batch dedupe; correct tracer docstring

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Diagnose and fix the `AppContext.shutdown()` aiosqlite connection leak

**Files:**
- Modify: `jarvis/main.py` and/or `jarvis/persistence/db.py` (diagnosis-dependent; production code only — tests must not change to mask the leak)
- Reference (read-only): `.superpowers/sdd/task-6-report.md` — full mechanism writeup + instrumentation recipe from the prior root-cause session

**Interfaces:**
- Consumes: the root cause already established: `tests/integration/test_main_smoke.py::test_bootstrap_disables_memory_for_non_sqlite_db_url`'s `ctx.shutdown()` explicitly stops only one of its two pooled aiosqlite connections; the second survives `await engine.dispose()` and is GC-reaped later (`aiosqlite.Connection.__del__` → `.stop()`) on an unrelated test's loop → intermittent `PytestUnhandledThreadExceptionWarning: RuntimeError: Event loop is closed`.
- Produces: every aiosqlite connection opened during that smoke test reaches an explicit stop before test end.

This task carries diagnostic latitude — the fix shape depends on what you find. Follow the decision tree; do not skip the verification gates.

- [ ] **Step 1: Reproduce with cheap instrumentation**

Read the "Teardown-race fix" section of `.superpowers/sdd/task-6-report.md` first. Re-apply its recipe: patch `.venv/lib/python3.12/site-packages/aiosqlite/core.py` with a cheap event log (append tuples to a module-level `DEBUG_EVENTS` list on `connect` / explicit `stop` / `__del__` / the `call_soon_threadsafe` failure — NO prints or stack captures in hot paths; heavier instrumentation hides the race), plus a throwaway autouse fixture in `tests/conftest.py` tagging events with the current test nodeid, dumped from a `pytest_sessionfinish` hook. Run `uv run pytest tests/integration/test_main_smoke.py` alone and confirm the smoke test opens N connections and stops N-1 (one `del`-only). Full-suite runs are only needed for final verification.

- [ ] **Step 2: Identify why the second connection survives `dispose()`**

Leading hypotheses to check in order:
1. A connection still **checked out** of the pool at `dispose()` time — dispose only closes checked-in connections. Find the holder: log pool checkout/checkin pairing per component (audit logger flush, seeds, ModelStore.load, create_all's `engine.begin()`, APScheduler) — note the report's warning that SQLAlchemy pool event listeners perturb GC timing; prefer logging inside the aiosqlite patch (connection identity → creation order) and correlating with `AppContext.shutdown()` ordering.
2. The async pool's **terminate-vs-close** path: `AsyncAdaptedQueuePool` dispose may terminate the underlying connection without running aiosqlite's full `close()` (which joins the worker thread).
State which hypothesis the evidence supports in your report before touching production code.

- [ ] **Step 3: Fix in production code**

Acceptable shapes, matching the diagnosis:
- If a component holds a connection across shutdown: reorder/await its teardown in `AppContext.shutdown()` (`jarvis/main.py`) so the connection is returned before `engine.dispose()`.
- If the pool's dispose path is the problem: configure SQLite engines in `jarvis/persistence/db.py` (e.g. `poolclass=NullPool` for `sqlite` URLs) so no pooled connection can outlive dispose. Note WAL mode is set per-connect via the event listener, so NullPool keeps working; state the perf trade-off (connection per checkout) in your report — for a single-user SQLite app it is acceptable.
- If the true fix requires upstream SQLAlchemy/aiosqlite changes: STOP, report BLOCKED with the evidence. Do not work around it in tests.

- [ ] **Step 4: Verify (acceptance criteria — all required)**

1. Instrumented run of `uv run pytest tests/integration/test_main_smoke.py`: every connection shows connect → explicit stop; zero `del`-only connections.
2. Revert ALL instrumentation (venv patch + conftest hook): `git status` clean apart from your production change; also `uv pip install --force-reinstall --no-deps aiosqlite` is acceptable to restore the venv file, or restore from the recorded original.
3. `uv run pytest` (bare, for the summary line) 5 consecutive times: every run ends `N passed, 1 warning` (the pre-existing discord/audioop deprecation only).
4. `uv run ruff check jarvis tests` clean.

- [ ] **Step 5: Commit**

```bash
git add jarvis/main.py jarvis/persistence/db.py
git commit -m "fix: stop all pooled aiosqlite connections at AppContext.shutdown

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

(Adjust the staged file list to what the diagnosis actually changed; message should name the real mechanism.)

---

### Task 7: Full-suite verification

**Files:**
- No new files; fix any fallout.

- [ ] **Step 1: Run `make check`; then `uv run pytest 2>&1 | tail -1` once**

Expected: lint clean; `N passed, 1 warning` (audioop only — Task 6 removed the flake).

- [ ] **Step 2: Fix any failures minimally, re-run until green; commit fixups**

```bash
git add -A -- ':!CLAUDE.md'
git commit -m "test: post-integration fixups

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

(Skip if nothing to fix.)
