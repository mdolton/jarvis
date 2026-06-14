"""MCPManager OAuth integration: replace, remove, isolation from YAML servers."""

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from agents import Agent
from agents.exceptions import UserError
from agents.mcp import MCPUtil
from agents.run_context import RunContextWrapper
from mcp.types import Tool

from jarvis.config.schema import MCPServersConfig
from jarvis.mcp.manager import MCPManager, _apply_runtime_policy_guard
from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import encrypt_blob, generate_key
from jarvis.oauth.store import MCPConnectionRepo, MCPProviderRepo
from jarvis.persistence.db import Base, create_engine, session_factory


class FakeSDKServer:
    """Duck-typed agents.mcp server used as a fake in tests."""

    def __init__(self, *, list_tools_returns=None, list_tools_raises=None):
        self.name = "fake"
        self._list_returns = list_tools_returns or []
        self._list_raises = list_tools_raises
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    async def list_tools(self, *args, **kwargs):
        if self._list_raises:
            raise self._list_raises
        return self._list_returns

    def _get_failure_error_function(self, agent_failure_error_function):
        return agent_failure_error_function

    def _get_needs_approval_for_tool(self, tool, agent):
        return False


class UnauthorizedOnceSDKServer(FakeSDKServer):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def call_tool(self, tool_name, arguments, meta=None):
        self.calls.append((tool_name, arguments, meta))
        raise UserError("Failed to call tool 'list_calendars': HTTP error 401")


class CallableSDKServer(FakeSDKServer):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def call_tool(self, tool_name, arguments, meta=None):
        self.calls.append((tool_name, arguments, meta))
        return "retried"


class RecordingToolSDKServer(FakeSDKServer):
    def __init__(self, tool_name: str):
        super().__init__(list_tools_returns=[Tool(name=tool_name, inputSchema={})])
        self.calls = []

    async def call_tool(self, tool_name, arguments, meta=None):
        self.calls.append((tool_name, arguments, meta))
        return "ok"


class FlakyAuthSDKServer(FakeSDKServer):
    """401s on the first call, then succeeds on the retry — same object.

    Models the real fix: after a 401 the manager refreshes the token in place
    and retries on the *same* live connection, rather than building a new one.
    """

    def __init__(self):
        super().__init__()
        self.calls = []
        self._failed = False

    async def call_tool(self, tool_name, arguments, meta=None):
        self.calls.append((tool_name, arguments, meta))
        if not self._failed:
            self._failed = True
            raise UserError("Failed to call tool 'list_calendars': HTTP error 401")
        return "retried"


class HangingSDKServer:
    """A server whose connect (aenter) never returns — models a transiently
    unresponsive remote like Google's early-access Calendar MCP endpoint."""

    def __init__(self):
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        import asyncio

        await asyncio.Event().wait()  # hang forever
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    async def list_tools(self):
        return []


class HangingCloseSDKServer(FakeSDKServer):
    async def __aexit__(self, *exc):
        import asyncio

        await asyncio.Event().wait()


class RefreshFlowStub:
    def __init__(self):
        self.refreshed = []

    async def refresh(self, connection_id):
        self.refreshed.append(connection_id)
        return {"Authorization": "Bearer fresh-token"}


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    async with f() as s:
        await seed_built_in_providers(s)
    yield f
    await engine.dispose()


async def test_replace_oauth_server_swaps_sdk_object(factory, monkeypatch):
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        first = FakeSDKServer()
        second = FakeSDKServer()
        builds = iter([first, second])

        def fake_build(url, headers, *, name, approval_policy=None, **_):
            assert approval_policy is not None
            return next(builds)

        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            fake_build,
        )

        await mgr.replace_oauth_server(
            "fastmail", url="https://api.fastmail.com/mcp",
            headers={"Authorization": "Bearer A1"},
        )
        assert mgr.agent_mcp_servers() == [first]
        assert first.entered

        await mgr.replace_oauth_server(
            "fastmail", url="https://api.fastmail.com/mcp",
            headers={"Authorization": "Bearer A2"},
        )
        assert mgr.agent_mcp_servers() == [second]
        assert second.entered
        # Old one is closed (eventually). Allow event loop to settle.
        import asyncio
        await asyncio.sleep(0)
        assert first.exited
    finally:
        await mgr.stop()


async def test_connection_tools_are_namespaced_by_label_and_provider(factory, monkeypatch):
    key = generate_key().encode()
    async with factory() as session:
        providers = MCPProviderRepo(session)
        for provider_key in ("cardtool", "ynab"):
            await providers.upsert(
                key=provider_key,
                display_name=provider_key.title(),
                kind="http",
                mcp_url=f"https://{provider_key}.example/mcp",
                builtin=False,
                auth_mode=None,
                oauth_metadata_url=None,
                pkce=True,
                send_resource_indicator=True,
                extra_auth_params={},
                default_scopes=[],
                header_names=[],
            )
        repo = MCPConnectionRepo(session)
        await repo.create(
            provider_key="cardtool",
            label="Personal",
            runtime_name="cardtool:personal",
        )
        await repo.create(
            provider_key="ynab",
            label="Personal",
            runtime_name="ynab:personal",
        )

    cardtool = RecordingToolSDKServer("list_categories")
    ynab = RecordingToolSDKServer("list_categories")

    def fake_build(url, headers, *, name, approval_policy=None, **_):
        return {"cardtool:personal": cardtool, "ynab:personal": ynab}[name]

    monkeypatch.setattr("jarvis.mcp.manager._build_streamable_http", fake_build)

    mgr = MCPManager(
        config=MCPServersConfig(servers=[]),
        session_factory=factory,
        secrets_key=key,
        catalog=ProviderCatalog(factory),
    )
    await mgr.start()
    try:
        servers = mgr.agent_mcp_servers()
        tools = await MCPUtil.get_all_function_tools(
            servers,
            convert_schemas_to_strict=False,
            run_context=RunContextWrapper(context=None),
            agent=Agent(name="test"),
        )

        assert {tool.name for tool in tools} == {
            "personal__cardtool__list_categories",
            "personal__ynab__list_categories",
        }
        assert "personal.cardtool" in mgr.agent_mcp_context()
        assert "personal.ynab" in mgr.agent_mcp_context()

        result = await servers[0].call_tool(
            "personal__cardtool__list_categories",
            {"account": "checking"},
        )

        assert result == "ok"
        assert cardtool.calls == [("list_categories", {"account": "checking"}, None)]
        assert ynab.calls == []
    finally:
        await mgr.stop()


async def test_oauth_server_call_401_refreshes_token_in_place_and_retries(factory, monkeypatch):
    flow = RefreshFlowStub()
    async with factory() as s:
        conn = await MCPConnectionRepo(s).create(
            provider_key="calendar", label="Cal", runtime_name="calendar", scopes=[])
    conn_id = conn.id
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(
        config=cfg, session_factory=factory, oauth_flow=flow, catalog=ProviderCatalog(factory)
    )
    await mgr.start()
    try:
        server = FlakyAuthSDKServer()
        builds = iter([server])
        captured = []

        def fake_build(
            url, headers, *, name, approval_policy=None, unauthorized_retry=None, token_holder=None
        ):
            sdk = next(builds)
            captured.append((url, headers, token_holder))
            if unauthorized_retry is not None:
                _apply_runtime_policy_guard(
                    sdk,
                    name,
                    approval_policy,
                    unauthorized_retry=unauthorized_retry,
                    unauthorized_detector=lambda exc: "401" in str(exc),
                )
            return sdk

        monkeypatch.setattr("jarvis.mcp.manager._build_streamable_http", fake_build)

        await mgr.replace_oauth_server(
            "calendar",
            url="https://calendarmcp.googleapis.com/mcp/v1",
            headers={"Authorization": "Bearer stale-token"},
        )
        # Exactly one build; the live token starts stale.
        assert len(captured) == 1
        assert mgr._token_holders["calendar"].get() == "stale-token"

        result = await mgr.agent_mcp_servers()[0].call_tool("list_calendars", {}, meta={"t": "1"})

        assert result == "retried"
        assert flow.refreshed == [conn_id]
        # No second build and no swap: the SAME server is retried, with the
        # refreshed token applied to its holder in place.
        assert len(captured) == 1
        assert mgr.agent_mcp_servers() == [server]
        assert mgr._token_holders["calendar"].get() == "fresh-token"
        assert server.calls == [
            ("list_calendars", {}, {"t": "1"}),
            ("list_calendars", {}, {"t": "1"}),
        ]
    finally:
        await mgr.stop()


async def test_token_holder_request_hook_injects_current_bearer():
    """The live httpx client must send whatever token the holder currently has,
    so a refresh that mutates the holder is picked up without any reconnect."""
    import httpx

    from jarvis.mcp.manager import (
        _TokenHolder,
        _tracking_httpx_client_factory,
        _UnauthorizedTracker,
    )

    holder = _TokenHolder("tok-1")
    client = _tracking_httpx_client_factory(_UnauthorizedTracker(), holder)(
        headers=None, timeout=None, auth=None
    )
    try:
        hooks = client.event_hooks["request"]
        req = httpx.Request("POST", "https://example.com/mcp")
        for h in hooks:
            await h(req)
        assert req.headers["authorization"] == "Bearer tok-1"

        holder.set("tok-2")  # simulate an in-place refresh
        req2 = httpx.Request("POST", "https://example.com/mcp")
        for h in hooks:
            await h(req2)
        assert req2.headers["authorization"] == "Bearer tok-2"
    finally:
        await client.aclose()


async def test_replace_oauth_server_aborts_on_list_tools_failure(factory, monkeypatch):
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        first = FakeSDKServer(list_tools_returns=[])
        broken = FakeSDKServer(list_tools_raises=RuntimeError("bad token"))
        builds = iter([first, broken])
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers, *, name, approval_policy=None, **_: next(builds),
        )
        await mgr.replace_oauth_server("fastmail", url="x", headers={"Authorization": "Bearer A1"})
        with pytest.raises(RuntimeError, match="bad token"):
            await mgr.replace_oauth_server("fastmail", url="x", headers={"Authorization": "Bearer A2"})
        # Old server still active.
        assert mgr.agent_mcp_servers() == [first]
        assert not first.exited
    finally:
        await mgr.stop()


async def test_remove_oauth_server_closes_and_drops(factory, monkeypatch):
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        sdk = FakeSDKServer()
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers, *, name, approval_policy=None, **_: sdk,
        )
        await mgr.replace_oauth_server("fastmail", url="x", headers={"Authorization": "Bearer A"})
        await mgr.remove_oauth_server("fastmail")
        assert mgr.agent_mcp_servers() == []
        assert sdk.exited
    finally:
        await mgr.stop()


async def test_start_iterates_catalog_and_attaches_oauth_server(factory, monkeypatch):
    """When an enabled Fastmail connection has tokens, start() builds the SDK server."""
    key = generate_key().encode()
    now = datetime.now(UTC)
    async with factory() as session:
        repo = MCPConnectionRepo(session)
        conn = await repo.create(
            provider_key="fastmail", label="Mail", runtime_name="fastmail:mail", scopes=[])
        await repo.set_tokens(
            conn.id,
            access_token_enc=encrypt_blob(b"AT", key),
            refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=now + timedelta(hours=1),
            scopes_granted=[],
        )

    sdk = FakeSDKServer()
    monkeypatch.setattr(
        "jarvis.mcp.manager._build_streamable_http",
        lambda url, headers, *, name, approval_policy=None, **_: sdk,
    )

    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(
        config=cfg, session_factory=factory, secrets_key=key, catalog=ProviderCatalog(factory)
    )
    await mgr.start()
    try:
        assert sdk.entered
        assert mgr.agent_mcp_servers() == [sdk]
    finally:
        await mgr.stop()


async def test_start_attaches_connected_manual_provider(factory, monkeypatch):
    """A connected manual-mode (gmail) connection must be attached at boot, not skipped."""
    key = generate_key().encode()
    now = datetime.now(UTC)
    async with factory() as session:
        repo = MCPConnectionRepo(session)
        conn = await repo.create(
            provider_key="gmail", label="Mail", runtime_name="gmail:mail", scopes=[])
        await repo.set_tokens(
            conn.id,
            access_token_enc=encrypt_blob(b"AT", key),
            refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=now + timedelta(hours=1),
            scopes_granted=[],
        )

    captured = {}
    sdk = FakeSDKServer()

    def fake_build(url, headers, *, name, approval_policy=None, **_):
        captured["url"] = url
        captured["approval_policy"] = approval_policy
        return sdk

    monkeypatch.setattr("jarvis.mcp.manager._build_streamable_http", fake_build)

    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(
        config=cfg, session_factory=factory, secrets_key=key, catalog=ProviderCatalog(factory)
    )
    await mgr.start()
    try:
        assert sdk.entered
        assert mgr.agent_mcp_servers() == [sdk]
        assert captured["url"] == "https://gmailmcp.googleapis.com/mcp/v1"
        assert captured["approval_policy"] is not None
    finally:
        await mgr.stop()


async def test_hung_replace_times_out_and_does_not_block_remove(factory, monkeypatch):
    """A connect that hangs must not wedge the lifecycle loop.

    Regression for the Google Calendar wedge: a transiently unresponsive
    endpoint hung _do_replace_oauth, head-of-line-blocking every later command
    so Disconnect (remove) never ran. The replace must time out, and a
    subsequent remove must complete promptly.
    """
    import asyncio

    mgr = MCPManager(
        config=MCPServersConfig(servers=[]),
        session_factory=factory,
        connect_timeout=0.3,
    )
    await mgr.start()
    try:
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers, *, name, approval_policy=None, **_: HangingSDKServer(),
        )
        with pytest.raises(TimeoutError):
            await mgr.replace_oauth_server(
                "calendar", url="x", headers={"Authorization": "Bearer X"}
            )
        # The loop must be free now — remove completes well within the timeout.
        await asyncio.wait_for(mgr.remove_oauth_server("calendar"), timeout=2.0)
        assert mgr.agent_mcp_servers() == []
    finally:
        await mgr.stop()


async def test_replace_oauth_server_times_out_hung_old_connection_close(factory, monkeypatch):
    """A wedged old stream close must not hang reconnect/callback after the new token is saved."""
    import asyncio

    mgr = MCPManager(
        config=MCPServersConfig(servers=[]),
        session_factory=factory,
        close_timeout=0.1,
    )
    await mgr.start()
    try:
        old = HangingCloseSDKServer()
        new = FakeSDKServer()
        builds = iter([old, new])
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers, *, name, approval_policy=None, **_: next(builds),
        )

        await mgr.replace_oauth_server("calendar", url="x", headers={"Authorization": "old"})
        await asyncio.wait_for(
            mgr.replace_oauth_server("calendar", url="x", headers={"Authorization": "new"}),
            timeout=1.0,
        )

        assert mgr.agent_mcp_servers() == [new]
    finally:
        if mgr._loop_task is not None and not mgr._loop_task.done():
            await mgr.stop()
        elif mgr._loop_task is not None:
            mgr._loop_task.cancel()
            with contextlib.suppress(BaseException):
                await mgr._loop_task


async def test_start_skips_oauth_provider_without_credentials(factory, monkeypatch):
    """An enabled OAuth connection without tokens must NOT be attached at boot."""
    key = generate_key().encode()
    async with factory() as session:
        await MCPConnectionRepo(session).create(
            provider_key="fastmail", label="Mail", runtime_name="fastmail:mail", scopes=[])
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(
        config=cfg, session_factory=factory, secrets_key=key, catalog=ProviderCatalog(factory)
    )
    builds = []
    monkeypatch.setattr(
        "jarvis.mcp.manager._build_streamable_http",
        lambda url, headers, *, name, approval_policy=None, **_: builds.append(1),
    )
    await mgr.start()
    try:
        assert builds == []
        assert mgr.agent_mcp_servers() == []
    finally:
        await mgr.stop()
