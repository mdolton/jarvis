# Medium-Severity Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the five medium-severity review findings: url_override consistency, callback-attach timeout semantics, concurrent-refresh coalescing, action resume timeout, and pending OAuth state TTL.

**Architecture:** All OAuth attach paths route URL computation through `MCPManager.connect_connection` (which now honors `url_override`); the callback route reuses it and treats attach timeout as "pending", not failure. `OAuthFlow.refresh` gains a per-connection lock with a 30s recency-window short-circuit. `ActionService` gains a `run_timeout_sec` bound. The pending-state TTL is enforced in `handle_callback`, sharing one constant with the sweep.

**Tech Stack:** Python 3.12, httpx (+ MockTransport in tests), OpenAI Agents SDK, FastAPI, SQLAlchemy async + SQLite, pytest (`asyncio_mode = auto`).

Spec: `docs/superpowers/specs/2026-07-08-medium-severity-fixes-design.md` (the CLAUDE.md docs edit — spec Fix F — is already committed as `d380673`; no task needed).

## Global Constraints

- Use `uv run` for every command; there is no activated venv.
- Pytest runs in `asyncio_mode = auto`: write `async def test_*` with NO `@pytest.mark.asyncio` decorator.
- DB access only through repository classes; feature code never opens raw sessions.
- Ruff: line length 100, target py312. Run `uv run ruff check jarvis tests` before each commit.
- Every commit message ends with the trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Exact values from the spec: `POST_CALLBACK_ATTACH_TIMEOUT` stays `10.0`; refresh coalesce window default `30.0` (constructor param `refresh_coalesce_window_sec`); pending TTL constant `PENDING_STATE_TTL_SEC = 600` in `jarvis/oauth/store.py`; `ActionService` timeout wired from `cfg.jarvis.idle_timeout_sec` in `main.py`.
- On Python 3.12 `asyncio.wait_for`/`asyncio.timeout` raise the **builtin** `TimeoutError`; except-clauses must use bare `TimeoutError`.

---

### Task 1: `MCPManager` honors `url_override` on OAuth attach paths

**Files:**
- Modify: `jarvis/mcp/manager.py` (two lines: `connect_connection` oauth branch ~line 351, `_bootstrap_connections` oauth branch ~line 471)
- Test: `tests/integration/test_mcp_manager_connections.py` (append)

**Interfaces:**
- Consumes: existing `MCPConnectionRow.url_override`, `ProviderEntry.mcp_url`.
- Produces: `connect_connection(conn)` and boot attach resolve the URL as `conn.url_override or entry.mcp_url` for OAuth connections (http/sse already did). Task 2's route relies on `connect_connection` being the single URL-resolution point.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_mcp_manager_connections.py` (the file already imports `datetime`/`UTC`/`timedelta`, `patch`, `MCPServersConfig`, `MCPManager`, `ProviderCatalog`, `seed_built_in_providers`, `encrypt_blob`, `generate_key`, `MCPConnectionRepo`, and defines `_FakeSDK` and the `factory` fixture):

```python
async def test_bootstrap_oauth_attach_honors_url_override(factory):
    key = generate_key().encode()
    async with factory() as s:
        await MCPConnectionRepo(s).create(
            provider_key="calendar",
            label="Alt",
            runtime_name="calendar:alt",
            url_override="https://alt.example/mcp",
        )
    async with factory() as s:
        conn = await MCPConnectionRepo(s).get_by_runtime_name("calendar:alt")
        await MCPConnectionRepo(s).set_tokens(
            conn.id,
            access_token_enc=encrypt_blob(b"AT", key),
            refresh_token_enc=None,
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes_granted=[],
        )

    captured = {}

    def fake_build(url, headers, **kwargs):
        captured["url"] = url
        return _FakeSDK()

    mgr = MCPManager(
        config=MCPServersConfig(servers=[]),
        session_factory=factory,
        secrets_key=key,
        oauth_flow=None,
        catalog=ProviderCatalog(factory),
    )
    with patch("jarvis.mcp.manager._build_streamable_http", side_effect=fake_build):
        await mgr.start()
    try:
        assert captured["url"] == "https://alt.example/mcp"
    finally:
        await mgr.stop()


async def test_connect_connection_honors_url_override(factory):
    key = generate_key().encode()
    async with factory() as s:
        await MCPConnectionRepo(s).create(
            provider_key="calendar",
            label="Alt2",
            runtime_name="calendar:alt2",
            url_override="https://alt2.example/mcp",
            enabled=False,  # keep bootstrap from attaching it; we call connect_connection directly
        )
    async with factory() as s:
        conn = await MCPConnectionRepo(s).get_by_runtime_name("calendar:alt2")
        await MCPConnectionRepo(s).set_tokens(
            conn.id,
            access_token_enc=encrypt_blob(b"AT", key),
            refresh_token_enc=None,
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes_granted=[],
        )
        conn = await MCPConnectionRepo(s).get_by_runtime_name("calendar:alt2")

    captured = {}

    def fake_build(url, headers, **kwargs):
        captured["url"] = url
        return _FakeSDK()

    mgr = MCPManager(
        config=MCPServersConfig(servers=[]),
        session_factory=factory,
        secrets_key=key,
        oauth_flow=None,
        catalog=ProviderCatalog(factory),
    )
    with patch("jarvis.mcp.manager._build_streamable_http", side_effect=fake_build):
        await mgr.start()
        try:
            await mgr.connect_connection(conn)
        finally:
            await mgr.stop()
    assert captured["url"] == "https://alt2.example/mcp"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_mcp_manager_connections.py -q`
Expected: the two new tests FAIL with `AssertionError` — captured URL is `https://calendarmcp.googleapis.com/mcp/v1` (the catalog `mcp_url`), not the override.

- [ ] **Step 3: Implement in `jarvis/mcp/manager.py`**

In `connect_connection`, the oauth branch currently reads:

```python
            await self.replace_oauth_server(
                conn.runtime_name,
                url=entry.mcp_url,
                headers={"Authorization": f"Bearer {token}"},
                oauth=True,
                tool_namespace=_tool_namespace_for_runtime_name(conn.runtime_name),
            )
```

Change `url=entry.mcp_url` to `url=conn.url_override or entry.mcp_url`.

In `_bootstrap_connections`, the oauth branch currently reads:

```python
                    await self.replace_oauth_server(
                        conn.runtime_name,
                        url=entry.mcp_url,
                        headers={"Authorization": f"Bearer {token}"},
                        oauth=True,
                        tool_namespace=_tool_namespace_for_runtime_name(conn.runtime_name),
                    )
```

Change `url=entry.mcp_url` to `url=conn.url_override or entry.mcp_url`.

(Do NOT touch the http/sse branches — they already honor the override.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_mcp_manager_connections.py -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/mcp/manager.py tests/integration/test_mcp_manager_connections.py
git commit -m "fix: honor url_override on OAuth connection attach paths

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Callback route reuses `connect_connection`; attach timeout renders "pending", not failure

**Files:**
- Modify: `jarvis/web/routes/oauth.py` (the post-callback attach block in `oauth_callback`)
- Modify: `jarvis/web/templates/oauth_callback.html` (add `pending` outcome)
- Test: `tests/integration/test_web_oauth.py` (rewrite `_ManagerStub` family + affected tests, add pending-status assertions)

**Interfaces:**
- Consumes: Task 1's `MCPManager.connect_connection(conn)` (resolves URL/token itself from the row).
- Produces: callback attach behavior — `TimeoutError` → HTTP 200 `pending` page, connection status untouched; other exceptions → HTTP 500, `needs_reauth` (unchanged); success → 200 success page. `POST_CALLBACK_ATTACH_TIMEOUT` remains `10.0`.

- [ ] **Step 1: Update the manager stubs and rewrite the affected tests**

In `tests/integration/test_web_oauth.py`:

1a. Replace the `_ManagerStub` base class:

```python
class _ManagerStub:
    def __init__(self):
        self.connected = []

    async def connect_connection(self, conn):
        self.connected.append(conn.runtime_name)
```

1b. Replace `_ManagerStubReplaceRaises` and `_ManagerStubReplaceHangs`:

```python
class _ManagerStubConnectRaises(_ManagerStub):
    async def connect_connection(self, conn):
        raise RuntimeError("attach boom")


class _ManagerStubConnectHangs(_ManagerStub):
    async def connect_connection(self, conn):
        await asyncio.Event().wait()
```

Update their two usages: `test_callback_marks_needs_reauth_when_attach_fails` uses `_ManagerStubConnectRaises()`, `test_callback_times_out_hung_mcp_attach` uses `_ManagerStubConnectHangs()`. (`_ManagerStubWithRemove` and `_ManagerStubRemoveRaises` extend `_ManagerStub` untouched — they only add `remove_oauth_server`.)

1c. In `test_callback_happy_path_renders_success_and_swaps_server`, replace the final assertion:

```python
    assert ctx.mcp_manager.replaced == [
        ("fastmail:default", "https://api.fastmail.com/mcp", {"Authorization": "Bearer AT"}),
    ]
```

with:

```python
    assert ctx.mcp_manager.connected == ["fastmail:default"]
```

1d. Rewrite the tail of `test_callback_times_out_hung_mcp_attach` — timeout is now "pending", not failure. Replace:

```python
    assert r2.status_code == 500
    assert "MCP attach failed" in r2.text
    async with factory() as session:
        row = await MCPConnectionRepo(session).get(conn.id)
        assert row is not None
        assert row.status == "needs_reauth"
```

with:

```python
    assert r2.status_code == 200
    assert "still in progress" in r2.text
    async with factory() as session:
        row = await MCPConnectionRepo(session).get(conn.id)
        assert row is not None
        # Tokens were stored and the attach is merely pending — status must NOT
        # be downgraded to needs_reauth by a timeout.
        assert row.status == "connected"
```

Also update that test's docstring to: `"""A slow MCP attach must not hang the browser NOR mark the connection needs_reauth."""`

(`test_callback_marks_needs_reauth_when_attach_fails` keeps all its assertions — a real attach exception still yields 500 + `needs_reauth`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_web_oauth.py -q`
Expected: FAILures — the route still calls `replace_oauth_server` (stubs no longer define it, so the happy-path test errors with `AttributeError`), and the timeout test gets 500/needs_reauth instead of 200/connected.

- [ ] **Step 3: Implement the route change in `jarvis/web/routes/oauth.py`**

Add `MCPConnectionRepo` usage for the row load (already imported). Replace the whole block after `handle_callback` — from `# Attach the SDK server with fresh headers.` through the final success `TemplateResponse` — with:

```python
    entry = await ctx.catalog.get(result.provider_key)
    async with ctx.session_factory() as session:
        conn = await MCPConnectionRepo(session).get(result.connection_id)
    if ctx.mcp_manager is not None and conn is not None:
        try:
            # connect_connection resolves url_override/token from the row itself,
            # so this route no longer hand-rolls the attach.
            await asyncio.wait_for(
                ctx.mcp_manager.connect_connection(conn),
                timeout=POST_CALLBACK_ATTACH_TIMEOUT,
            )
        except TimeoutError:
            # The attach command is queued on the MCP lifecycle task and keeps
            # running after this wait gives up — a timeout is "still in
            # progress", never a failure. Do not touch connection status; the
            # dashboard's runtime status reflects the eventual outcome.
            _log.warning(
                "post-callback MCP attach still pending for %s", result.runtime_name
            )
            return templates.TemplateResponse(
                request,
                "oauth_callback.html",
                {"outcome": "pending", "provider": entry.display_name, "message": ""},
            )
        except Exception as e:
            _log.exception(
                "post-callback MCP attach failed for %s", result.runtime_name
            )
            # Tokens are stored but the server never came up. Don't leave the
            # connection claiming "connected" with no tools — flag it so the
            # dashboard tells the truth and the user can retry.
            async with ctx.session_factory() as session:
                await MCPConnectionRepo(session).set_status(
                    result.connection_id,
                    status="needs_reauth",
                    last_error=f"MCP attach failed: {e}",
                )
            return templates.TemplateResponse(
                request,
                "oauth_callback.html",
                {
                    "outcome": "error",
                    "provider": entry.display_name,
                    "message": f"connected, but MCP attach failed: {e}",
                },
                status_code=500,
            )

    return templates.TemplateResponse(
        request,
        "oauth_callback.html",
        {"outcome": "success", "provider": entry.display_name, "message": ""},
    )
```

The route no longer calls `ctx.oauth_flow.current_headers(...)` — delete that line.

- [ ] **Step 4: Add the `pending` outcome to `jarvis/web/templates/oauth_callback.html`**

Insert between the `success` and `declined` branches:

```html
  {% elif outcome == "pending" %}
    <h2>Connected to {{ provider }}.</h2>
    <p>MCP attach is still in progress — check the <a href="/mcp">MCP page</a> for status.</p>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_web_oauth.py -q`
Expected: all pass.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/web/routes/oauth.py jarvis/web/templates/oauth_callback.html tests/integration/test_web_oauth.py
git commit -m "fix: callback attach reuses connect_connection; timeout renders pending, not needs_reauth

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Per-connection refresh lock with recency-window short-circuit

**Files:**
- Modify: `jarvis/oauth/flow.py`
- Test: `tests/integration/test_oauth_flow.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `OAuthFlow.__init__` gains keyword `refresh_coalesce_window_sec: float = 30.0`. `refresh(connection_id)` keeps its signature and error taxonomy; concurrent callers within the window get the same headers from one token-endpoint exchange.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_oauth_flow.py` (the file already has `db_factory`, `fastmail_metadata_payload`, `make_client`, `_make_connection`, `generate_key`, `ProviderCatalog`, `OAuthFlow`, `parse_qs`/`urlparse` imports; add `import asyncio` to the top-of-file imports if missing):

```python
def _refresh_counting_handler(fastmail_metadata_payload, refresh_calls):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            form = dict(p.split("=", 1) for p in request.read().decode().split("&"))
            if form["grant_type"] == "authorization_code":
                return httpx.Response(
                    200,
                    json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600},
                )
            refresh_calls["count"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"AT{refresh_calls['count'] + 1}",
                    "refresh_token": f"RT{refresh_calls['count'] + 1}",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(404)

    return handler


async def test_concurrent_refresh_coalesces_to_one_exchange(db_factory, fastmail_metadata_payload):
    refresh_calls = {"count": 0}
    handler = _refresh_counting_handler(fastmail_metadata_payload, refresh_calls)
    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    state = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=state, code="abc")

    first, second = await asyncio.gather(flow.refresh(conn.id), flow.refresh(conn.id))

    assert refresh_calls["count"] == 1
    assert first == second == {"Authorization": "Bearer AT2"}


async def test_refresh_outside_window_hits_endpoint_again(db_factory, fastmail_metadata_payload):
    refresh_calls = {"count": 0}
    handler = _refresh_counting_handler(fastmail_metadata_payload, refresh_calls)
    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory),
                     refresh_coalesce_window_sec=0.0)
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    state = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=state, code="abc")

    assert await flow.refresh(conn.id) == {"Authorization": "Bearer AT2"}
    assert await flow.refresh(conn.id) == {"Authorization": "Bearer AT3"}
    assert refresh_calls["count"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_oauth_flow.py -q`
Expected: `test_refresh_outside_window_hits_endpoint_again` FAILS with `TypeError: OAuthFlow.__init__() got an unexpected keyword argument 'refresh_coalesce_window_sec'`; `test_concurrent_refresh_coalesces_to_one_exchange` FAILS with `refresh_calls["count"] == 2` (both callers hit the endpoint today).

- [ ] **Step 3: Implement in `jarvis/oauth/flow.py`**

3a. Add to the module imports: `import asyncio` and `import time` (alongside the existing `import base64` / `import logging`).

3b. Extend `__init__` — add the keyword parameter and state (after the existing assignments):

```python
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession] | None,
        base_url: str,
        secrets_key: bytes,
        catalog: ProviderCatalog | None = None,
        refresh_coalesce_window_sec: float = 30.0,
    ) -> None:
        self._http = http_client
        self._session_factory = session_factory
        self._base_url = base_url.rstrip("/")
        self._secrets_key = secrets_key
        self._catalog = catalog
        # Per-connection serialization of refresh() plus a recency window:
        # concurrent 401s coalesce onto one token exchange instead of burning a
        # single-use rotated refresh token (invalid_grant -> spurious
        # needs_reauth). A recency window (NOT a token_expires_at check) so a
        # server-side-revoked-but-unexpired token still reaches the endpoint
        # and produces the correct invalid_grant signal.
        self._refresh_window = refresh_coalesce_window_sec
        self._refresh_locks: dict[UUID, asyncio.Lock] = {}
        self._last_refresh: dict[UUID, tuple[float, dict[str, str]]] = {}
```

3c. Rename the existing `refresh` method to `_refresh_locked` (body unchanged, including its docstring) and add the new `refresh` in its place:

```python
    async def refresh(self, connection_id: UUID) -> dict[str, str]:
        """Refresh tokens, serialized per connection with a recency window.

        Concurrent callers coalesce: whoever wins the lock does the exchange;
        callers that acquire the lock within ``refresh_coalesce_window_sec`` of
        a successful refresh get that refresh's headers without a second token
        exchange. Failed refreshes never populate the window.
        """
        lock = self._refresh_locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            recent = self._last_refresh.get(connection_id)
            if recent is not None and time.monotonic() - recent[0] < self._refresh_window:
                return dict(recent[1])
            headers = await self._refresh_locked(connection_id)
            self._last_refresh[connection_id] = (time.monotonic(), dict(headers))
            return dict(headers)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_oauth_flow.py tests/integration/test_oauth_flow_connections.py tests/integration/test_oauth_jobs.py -q`
Expected: all pass (existing refresh tests exercise the lock path transparently).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow.py
git commit -m "fix: serialize OAuth refresh per connection with recency-window coalescing

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `ActionService` resume timeout

**Files:**
- Modify: `jarvis/actions/service.py`
- Modify: `jarvis/main.py` (ActionService construction)
- Test: `tests/integration/test_action_service.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ActionService.__init__` gains keyword `run_timeout_sec: float | None = None`; a timed-out resume marks the action `failed` (error starts with `TimeoutError`) and re-raises.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_action_service.py` (the file already defines `infra`, `_action`, `_FakeRunState`, `_FakeResult`, and imports `AsyncMock`, `SimpleNamespace`, `ActionRepo`, `ActionService`, `LLMConfig`; add `import asyncio` and `import pytest` to the top-of-file imports if missing):

```python
async def test_resume_timeout_marks_failed(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory)
    canonical_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    state = _FakeRunState([canonical_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )

    async def hung_run(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr("jarvis.actions.service.Runner.run", hung_run)

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=SimpleNamespace(route=AsyncMock()),
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
        run_timeout_sec=0.05,
    )

    with pytest.raises(TimeoutError):
        await service.approve(action.id)

    async with factory() as s:
        row = await ActionRepo(s).get(action.id)
    assert row.status == "failed"
    assert "TimeoutError" in (row.error or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_action_service.py::test_resume_timeout_marks_failed -q`
Expected: FAIL with `TypeError: ActionService.__init__() got an unexpected keyword argument 'run_timeout_sec'`.

- [ ] **Step 3: Implement**

3a. In `jarvis/actions/service.py`, `__init__`: add parameter `run_timeout_sec: float | None = None` (after `memory_service`) and store `self._run_timeout_sec = run_timeout_sec`.

3b. In `_decide`, replace:

```python
            sdk_result = await Runner.run(
                agent,
                run_state,
                run_config=RunConfig(workflow_name="jarvis-action-resume"),
            )
```

with:

```python
            if self._run_timeout_sec is None:
                sdk_result = await Runner.run(
                    agent,
                    run_state,
                    run_config=RunConfig(workflow_name="jarvis-action-resume"),
                )
            else:
                # A wedged resume must not hold the (shielded) approve request
                # forever with the action stuck in 'running'. TimeoutError flows
                # into the except-path below: mark failed, route notice, re-raise.
                async with asyncio.timeout(self._run_timeout_sec):
                    sdk_result = await Runner.run(
                        agent,
                        run_state,
                        run_config=RunConfig(workflow_name="jarvis-action-resume"),
                    )
```

(`asyncio` is already imported in `service.py`.)

3c. In `jarvis/main.py`, add `run_timeout_sec=cfg.jarvis.idle_timeout_sec,` to the `ActionService(...)` construction (after `mcp_servers_provider=...`), mirroring the Scheduler's bound.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_action_service.py tests/integration/test_main_smoke.py -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/actions/service.py jarvis/main.py tests/integration/test_action_service.py
git commit -m "fix: bound action-resume runs with run_timeout_sec

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Enforce pending OAuth state TTL at callback time

**Files:**
- Modify: `jarvis/oauth/store.py` (constant + sweep default)
- Modify: `jarvis/oauth/flow.py` (`handle_callback` age check)
- Modify: `jarvis/scheduler/oauth_jobs.py` (sweep default uses the constant)
- Test: `tests/integration/test_oauth_flow.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `PENDING_STATE_TTL_SEC = 600` exported from `jarvis/oauth/store.py`; `handle_callback` raises `OAuthCallbackError` for pending rows older than the TTL and deletes them.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_oauth_flow.py` (add `from datetime import UTC, datetime, timedelta` to the imports if not present; `OAuthCallbackError` may need adding to the `jarvis.oauth.flow` import):

```python
async def test_handle_callback_rejects_expired_pending_state(db_factory, fastmail_metadata_payload):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}
            )
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    state = parse_qs(urlparse(consent_url).query)["state"][0]

    # Backdate the pending row past the TTL.
    from sqlalchemy import update as sa_update

    from jarvis.persistence.models import MCPPendingRow

    async with db_factory() as session:
        await session.execute(
            sa_update(MCPPendingRow)
            .where(MCPPendingRow.state == state)
            .values(created_at=datetime.now(UTC) - timedelta(seconds=700))
        )
        await session.commit()

    from jarvis.oauth.flow import OAuthCallbackError
    from jarvis.oauth.store import MCPPendingRepo

    with pytest.raises(OAuthCallbackError, match="expired"):
        await flow.handle_callback(state=state, code="abc")

    async with db_factory() as session:
        assert await MCPPendingRepo(session).get(state) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_oauth_flow.py::test_handle_callback_rejects_expired_pending_state -q`
Expected: FAIL — `handle_callback` currently accepts the 700s-old state and completes (no `OAuthCallbackError` raised).

- [ ] **Step 3: Implement**

3a. `jarvis/oauth/store.py` — add below the imports:

```python
# How long an in-flight authorization (mcp_pending row) stays valid. Enforced
# at use in OAuthFlow.handle_callback; the daily sweep is garbage collection.
PENDING_STATE_TTL_SEC = 600
```

and change `sweep_expired`'s signature default from `ttl_seconds: int = 600` to `ttl_seconds: int = PENDING_STATE_TTL_SEC`.

3b. `jarvis/scheduler/oauth_jobs.py` — add `PENDING_STATE_TTL_SEC` to the existing `from jarvis.oauth.store import ...` import and change `oauth_pending_sweep`'s default from `ttl_seconds: int = 600` to `ttl_seconds: int = PENDING_STATE_TTL_SEC`.

3c. `jarvis/oauth/flow.py`:
- Add `PENDING_STATE_TTL_SEC` to the existing `from jarvis.oauth.store import ...` import.
- In `handle_callback`, directly after the `if pending is None: raise ...` block, insert:

```python
        # Enforce the TTL at use — the daily sweep alone would leave a state
        # valid for up to ~24h.
        if pending.created_at < datetime.now(UTC) - timedelta(seconds=PENDING_STATE_TTL_SEC):
            async with self._session_factory() as session:
                await MCPPendingRepo(session).delete(state)
            raise OAuthCallbackError(f"unknown or expired state {state!r}")
```

(`datetime`, `timedelta`, `UTC`, and `MCPPendingRepo` are already imported in `flow.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_oauth_flow.py tests/integration/test_oauth_jobs.py tests/integration/test_mcp_pending_repo.py -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/oauth/store.py jarvis/oauth/flow.py jarvis/scheduler/oauth_jobs.py tests/integration/test_oauth_flow.py
git commit -m "fix: enforce pending OAuth state TTL in handle_callback

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Full-suite verification

**Files:**
- No new files; fix any fallout uncovered by the full run.

**Interfaces:**
- Consumes: everything above.
- Produces: green `make check`.

- [ ] **Step 1: Run lint + the full test suite**

Run: `make check` and check the summary with `uv run pytest -q 2>&1 | tail -5` once.
Expected: 0 lint errors, all tests pass. Likely fallout spots: other tests that construct `_Ctx`/manager stubs around the callback route (`tests/integration/test_web_mcp_e2e.py` if it exercises the callback), and anything asserting `replace_oauth_server` was called by the callback path.

- [ ] **Step 2: Fix any failures, re-run until green**

Apply minimal fixes consistent with the tasks above (e.g. stubs gaining `connect_connection`).

- [ ] **Step 3: Commit any fixups**

```bash
git add -A -- ':!CLAUDE.md'
git commit -m "test: adapt remaining stubs to connect_connection callback path

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

(Skip the commit if Step 1 was already green with nothing to fix.)
