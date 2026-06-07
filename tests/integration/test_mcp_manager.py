"""MCPManager integration tests using a real in-process stdio MCP server."""

import asyncio
import re
import sys
from types import MethodType

import pytest
from agents.exceptions import UserError
from agents.mcp import MCPUtil
from mcp.types import Tool

from jarvis.config.schema import MCPServerConfig, MCPServersConfig
from jarvis.mcp.manager import (
    MCPManager,
    _apply_runtime_policy_guard,
    _build_sdk_server,
    _build_streamable_http,
)
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo

# --- A minimal stdio MCP server, written as a standalone script ---
# We use the official `mcp` SDK (a transitive dep of openai-agents) to spin
# up a one-tool server that we then ask MCPManager to connect to.
_SERVER_SCRIPT = """
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

app = Server("test-server")

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Echo the input back",
            inputSchema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    return [TextContent(type="text", text=arguments.get("text", ""))]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

asyncio.run(main())
"""


@pytest.fixture
async def engine_and_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield engine, factory
    await engine.dispose()


@pytest.fixture
def test_server_script(tmp_path):
    path = tmp_path / "mcp_server.py"
    path.write_text(_SERVER_SCRIPT)
    return path


async def test_mcp_manager_connects_and_catalogs_tools(engine_and_factory, test_server_script):
    _, factory = engine_and_factory

    cfg = MCPServersConfig(
        servers=[
            MCPServerConfig(
                name="test",
                transport="stdio",
                command=[sys.executable, str(test_server_script)],
            ),
        ],
    )

    manager = MCPManager(config=cfg, session_factory=factory)
    await manager.start()
    try:
        # Manager exposes the SDK server list for the Agent.
        sdk_servers = manager.agent_mcp_servers()
        assert len(sdk_servers) == 1

        # DB shadow: one server marked connected, one tool recorded.
        async with factory() as s:
            servers = await MCPServerRepo(s).list_all()
            assert len(servers) == 1
            assert servers[0].name == "test"
            assert servers[0].status == "connected"

            tools = await MCPToolRepo(s).list_for_server(servers[0].id)
            assert {t.name for t in tools} == {"echo"}
    finally:
        await manager.stop()


async def test_mcp_manager_records_failure_for_bad_command(engine_and_factory):
    _, factory = engine_and_factory

    cfg = MCPServersConfig(
        servers=[
            MCPServerConfig(
                name="broken",
                transport="stdio",
                command=["/nonexistent/binary"],
            ),
        ],
    )

    manager = MCPManager(config=cfg, session_factory=factory)
    await manager.start()
    try:
        async with factory() as s:
            servers = await MCPServerRepo(s).list_all()
            assert len(servers) == 1
            assert servers[0].status == "error"
            assert servers[0].last_error is not None
        # Failed server must NOT be exposed to the Agent.
        assert manager.agent_mcp_servers() == []
    finally:
        await manager.stop()


async def test_mcp_manager_handles_empty_config(engine_and_factory):
    _, factory = engine_and_factory
    cfg = MCPServersConfig(servers=[])
    manager = MCPManager(config=cfg, session_factory=factory)
    await manager.start()
    try:
        assert manager.agent_mcp_servers() == []
    finally:
        await manager.stop()


async def test_agent_mcp_servers_stable_identity(engine_and_factory, test_server_script):
    """Successive calls to agent_mcp_servers() return the same SDK objects."""
    import sys

    _, factory = engine_and_factory
    cfg = MCPServersConfig(
        servers=[
            MCPServerConfig(
                name="echo", transport="stdio", command=[sys.executable, str(test_server_script)]
            ),
        ],
    )
    manager = MCPManager(config=cfg, session_factory=factory)
    await manager.start()
    try:
        first_call = manager.agent_mcp_servers()
        second_call = manager.agent_mcp_servers()
        assert len(first_call) == 1
        assert first_call[0] is second_call[0]
    finally:
        await manager.stop()


async def test_stop_is_idempotent(engine_and_factory, test_server_script):
    """Calling stop() twice doesn't raise."""
    import sys

    _, factory = engine_and_factory
    cfg = MCPServersConfig(
        servers=[
            MCPServerConfig(
                name="echo", transport="stdio", command=[sys.executable, str(test_server_script)]
            ),
        ],
    )
    manager = MCPManager(config=cfg, session_factory=factory)
    await manager.start()
    await manager.stop()
    await manager.stop()  # second call should not raise


async def test_mcp_manager_rejects_yaml_server_named_after_catalog_key(engine_and_factory):
    """MCPManager.start raises if a YAML server name collides with an OAuth catalog key."""
    _, factory = engine_and_factory
    cfg = MCPServersConfig(
        servers=[
            MCPServerConfig(name="fastmail", transport="http", url="http://localhost"),
        ],
    )
    manager = MCPManager(config=cfg, session_factory=factory)
    with pytest.raises(ValueError, match="fastmail"):
        await manager.start()


async def test_mcp_manager_clear_policy_cache_delegates(engine_and_factory):
    _, factory = engine_and_factory
    manager = MCPManager(config=MCPServersConfig(servers=[]), session_factory=factory)
    manager._approval_policy = _RecordingCachePolicy()

    manager.clear_policy_cache()
    manager.clear_policy_cache("calendar")

    assert manager._approval_policy.calls == [("clear_cache", None), ("clear_server", "calendar")]


async def test_mcp_manager_clears_policy_cache_after_connect_tool_refresh(
    engine_and_factory, monkeypatch
):
    _, factory = engine_and_factory
    manager = MCPManager(config=MCPServersConfig(servers=[]), session_factory=factory)
    manager._approval_policy = _RecordingCachePolicy()
    monkeypatch.setattr(
        "jarvis.mcp.manager._build_sdk_server",
        lambda cfg, approval_policy: _FakeSdkServer([Tool(name="list_events", inputSchema={})]),
    )

    await manager._do_connect_one(
        MCPServerConfig(name="calendar", transport="stdio", command=[sys.executable])
    )

    assert ("clear_server", "calendar") in manager._approval_policy.calls


async def test_mcp_manager_clears_policy_cache_after_oauth_replace_and_remove(
    engine_and_factory, monkeypatch
):
    _, factory = engine_and_factory
    manager = MCPManager(config=MCPServersConfig(servers=[]), session_factory=factory)
    manager._approval_policy = _RecordingCachePolicy()
    monkeypatch.setattr(
        "jarvis.mcp.manager._build_streamable_http",
        lambda url, headers, *, name, approval_policy: _FakeSdkServer(
            [Tool(name="search_mail", inputSchema={})]
        ),
    )

    await manager._do_replace_oauth(provider_key="fastmail", url="http://localhost", headers={})
    await manager._do_remove_oauth("fastmail")

    assert manager._approval_policy.calls.count(("clear_server", "fastmail")) == 2


@pytest.mark.parametrize(
    "cfg",
    [
        MCPServerConfig(name="stdio-server", transport="stdio", command=[sys.executable]),
        MCPServerConfig(name="http-server", transport="http", url="http://localhost/mcp"),
        MCPServerConfig(name="sse-server", transport="sse", url="http://localhost/sse"),
    ],
)
async def test_sdk_server_builders_wire_approval_policy_for_all_transports(cfg):
    policy = _RecordingApprovalPolicy()
    sdk_server = _build_sdk_server(cfg, approval_policy=policy)
    tool = _SdkTool("send_email")

    assert await sdk_server._needs_approval_policy(None, None, tool) is True
    assert policy.calls == [("needs_approval", cfg.name, "send_email")]


@pytest.mark.parametrize(
    "cfg",
    [
        MCPServerConfig(name="stdio-server", transport="stdio", command=[sys.executable]),
        MCPServerConfig(name="http-server", transport="http", url="http://localhost/mcp"),
        MCPServerConfig(name="sse-server", transport="sse", url="http://localhost/sse"),
    ],
)
async def test_sdk_server_builders_wire_two_arg_tool_filter_for_all_transports(cfg):
    policy = _RecordingApprovalPolicy()
    sdk_server = _build_sdk_server(cfg, approval_policy=policy)
    tool = _SdkTool("send_email")

    assert await sdk_server.tool_filter(object(), tool) is False
    assert policy.calls == [("filter_tool", cfg.name, "send_email")]


async def test_mcp_util_get_function_tools_uses_two_arg_filter_without_dropping_tools():
    policy = _RecordingApprovalPolicy(filter_result=True)
    sdk_server = _build_sdk_server(
        MCPServerConfig(name="stdio-server", transport="stdio", command=[sys.executable]),
        approval_policy=policy,
    )
    tool = Tool(name="search_docs", inputSchema={})

    async def list_tools(self, run_context, agent):
        filter_context = _FilterContext(
            run_context=run_context,
            agent=agent,
            server_name=self.name,
        )
        if await self.tool_filter(filter_context, tool):
            return [tool]
        return []

    sdk_server.list_tools = MethodType(list_tools, sdk_server)

    function_tools = await MCPUtil.get_function_tools(
        sdk_server,
        convert_schemas_to_strict=False,
        run_context=object(),
        agent=object(),
    )

    assert [tool.name for tool in function_tools] == ["search_docs"]
    assert policy.calls == [("filter_tool", "stdio-server", "search_docs")]


@pytest.mark.parametrize("approval_result", [False, True])
async def test_mcp_util_get_function_tools_uses_public_approval_callback(approval_result):
    policy = _RecordingApprovalPolicy(approval_result=approval_result)
    sdk_server = _build_sdk_server(
        MCPServerConfig(name="stdio-server", transport="stdio", command=[sys.executable]),
        approval_policy=policy,
    )
    tool = Tool(name="send_email", inputSchema={})

    async def list_tools(self, run_context, agent):
        return [tool]

    sdk_server.list_tools = MethodType(list_tools, sdk_server)

    function_tools = await MCPUtil.get_function_tools(
        sdk_server,
        convert_schemas_to_strict=False,
        run_context=object(),
        agent=object(),
    )

    assert len(function_tools) == 1
    assert callable(function_tools[0].needs_approval)
    assert await function_tools[0].needs_approval(object(), {}, "call_1") is approval_result
    assert policy.calls == [("needs_approval", "stdio-server", "send_email")]


async def test_streamable_http_builder_wires_approval_policy():
    policy = _RecordingApprovalPolicy()
    sdk_server = _build_streamable_http(
        "http://localhost/mcp",
        {},
        name="oauth-server",
        approval_policy=policy,
    )
    tool = _SdkTool("delete_event")

    assert await sdk_server._needs_approval_policy(None, None, tool) is True
    assert await sdk_server.tool_filter(object(), tool) is False
    assert policy.calls == [
        ("needs_approval", "oauth-server", "delete_event"),
        ("filter_tool", "oauth-server", "delete_event"),
    ]


async def test_runtime_policy_guard_blocks_denied_stale_tool_call():
    policy = _RecordingApprovalPolicy(denied_names={"delete_event"})
    sdk_server = _CallableSdkServer()
    _apply_runtime_policy_guard(sdk_server, "calendar", policy)

    with pytest.raises(
        UserError,
        match=re.escape("MCP tool 'delete_event' on server 'calendar' is denied by policy."),
    ):
        await sdk_server.call_tool("delete_event", {"id": "1"})

    assert sdk_server.calls == []


async def test_runtime_policy_guard_delegates_allowed_tool_call():
    policy = _RecordingApprovalPolicy()
    sdk_server = _CallableSdkServer()
    _apply_runtime_policy_guard(sdk_server, "calendar", policy)

    result = await sdk_server.call_tool("list_events", {"calendar": "primary"}, meta={"trace": "1"})

    assert result == "called"
    assert sdk_server.calls == [("list_events", {"calendar": "primary"}, {"trace": "1"})]


async def test_runtime_policy_guard_refreshes_and_retries_once_on_unauthorized():
    policy = _RecordingApprovalPolicy()
    stale_server = _UnauthorizedSdkServer()
    refreshed_server = _CallableSdkServer()
    refreshes = []

    async def refresh_server():
        refreshes.append("calendar")
        return refreshed_server

    _apply_runtime_policy_guard(
        stale_server,
        "calendar",
        policy,
        unauthorized_retry=refresh_server,
        unauthorized_detector=lambda exc: "401" in str(exc),
    )

    result = await stale_server.call_tool(
        "list_calendars",
        {},
        meta={"trace": "calendar-401"},
    )

    assert result == "called"
    assert refreshes == ["calendar"]
    assert stale_server.calls == [("list_calendars", {}, {"trace": "calendar-401"})]
    assert refreshed_server.calls == [("list_calendars", {}, {"trace": "calendar-401"})]


async def test_runtime_policy_guard_refreshes_when_unauthorized_call_hangs_until_timeout():
    policy = _RecordingApprovalPolicy()
    stale_server = _HangingSdkServer()
    refreshed_server = _CallableSdkServer()
    refreshes = []

    async def refresh_server():
        refreshes.append("calendar")
        return refreshed_server

    _apply_runtime_policy_guard(
        stale_server,
        "calendar",
        policy,
        unauthorized_retry=refresh_server,
        unauthorized_detector=lambda exc: isinstance(exc, TimeoutError),
        tool_call_timeout=0.01,
    )

    result = await asyncio.wait_for(
        stale_server.call_tool("list_events", {"calendar": "primary"}, meta={"trace": "hung-401"}),
        timeout=1.0,
    )

    assert result == "called"
    assert refreshes == ["calendar"]
    assert stale_server.calls == [("list_events", {"calendar": "primary"}, {"trace": "hung-401"})]
    assert refreshed_server.calls == [
        ("list_events", {"calendar": "primary"}, {"trace": "hung-401"})
    ]


async def test_runtime_policy_guard_times_out_hung_unauthorized_retry():
    policy = _RecordingApprovalPolicy()
    stale_server = _UnauthorizedSdkServer()

    async def refresh_server():
        await asyncio.Event().wait()

    _apply_runtime_policy_guard(
        stale_server,
        "calendar",
        policy,
        unauthorized_retry=refresh_server,
        unauthorized_detector=lambda exc: "401" in str(exc),
        unauthorized_retry_timeout=0.01,
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            stale_server.call_tool("list_events", {"calendar": "primary"}),
            timeout=1.0,
        )

    assert stale_server.calls == [("list_events", {"calendar": "primary"}, None)]


class _RecordingApprovalPolicy:
    def __init__(self, *, filter_result=False, approval_result=True, denied_names=None):
        self.calls = []
        self._filter_result = filter_result
        self._approval_result = approval_result
        self._denied_names = set(denied_names or [])

    async def needs_approval(self, server_name, tool):
        self.calls.append(("needs_approval", server_name, tool.name))
        return self._approval_result

    async def filter_tool(self, server_name, tool):
        self.calls.append(("filter_tool", server_name, tool.name))
        return self._filter_result

    async def is_denied(self, server_name, tool_or_name):
        tool_name = tool_or_name if isinstance(tool_or_name, str) else tool_or_name.name
        self.calls.append(("is_denied", server_name, tool_name))
        return tool_name in self._denied_names


class _RecordingCachePolicy:
    def __init__(self):
        self.calls = []

    def clear_cache(self):
        self.calls.append(("clear_cache", None))

    def clear_server(self, server_name):
        self.calls.append(("clear_server", server_name))


class _FilterContext:
    def __init__(self, *, run_context, agent, server_name):
        self.run_context = run_context
        self.agent = agent
        self.server_name = server_name


class _FakeSdkServer:
    def __init__(self, tools):
        self._tools = tools
        self.tool_filter = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def list_tools(self):
        return self._tools


class _CallableSdkServer:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool_name, arguments, meta=None):
        self.calls.append((tool_name, arguments, meta))
        return "called"


class _UnauthorizedSdkServer:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool_name, arguments, meta=None):
        self.calls.append((tool_name, arguments, meta))
        raise UserError("Failed to call tool 'list_calendars': HTTP error 401")


class _HangingSdkServer:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool_name, arguments, meta=None):
        self.calls.append((tool_name, arguments, meta))
        await asyncio.Event().wait()


class _SdkTool:
    def __init__(self, name):
        self.name = name
        self.inputSchema = {}
        self.description = ""
        self.annotations = None
