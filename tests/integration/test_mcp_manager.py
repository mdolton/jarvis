"""MCPManager integration tests using a real in-process stdio MCP server."""

import sys

import pytest

from jarvis.config.schema import MCPServerConfig, MCPServersConfig
from jarvis.mcp.manager import MCPManager, _build_sdk_server, _build_streamable_http
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
            MCPServerConfig(name="echo", transport="stdio",
                            command=[sys.executable, str(test_server_script)]),
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
            MCPServerConfig(name="echo", transport="stdio",
                            command=[sys.executable, str(test_server_script)]),
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


async def test_sdk_dynamic_tool_filter_uses_two_arg_callback_without_dropping_tools():
    policy = _RecordingApprovalPolicy(filter_result=True)
    sdk_server = _build_sdk_server(
        MCPServerConfig(name="stdio-server", transport="stdio", command=[sys.executable]),
        approval_policy=policy,
    )
    tool = _SdkTool("search_docs")

    filtered = await sdk_server._apply_dynamic_tool_filter([tool], object(), object())

    assert filtered == [tool]
    assert policy.calls == [("filter_tool", "stdio-server", "search_docs")]


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


class _RecordingApprovalPolicy:
    def __init__(self, *, filter_result=False):
        self.calls = []
        self._filter_result = filter_result

    async def needs_approval(self, server_name, tool):
        self.calls.append(("needs_approval", server_name, tool.name))
        return True

    async def filter_tool(self, server_name, tool):
        self.calls.append(("filter_tool", server_name, tool.name))
        return self._filter_result


class _SdkTool:
    def __init__(self, name):
        self.name = name
        self.inputSchema = {}
        self.description = ""
        self.annotations = None
