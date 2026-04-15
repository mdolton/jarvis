"""MCPManager integration tests using a real in-process stdio MCP server."""

import sys

import pytest

from jarvis.config.schema import MCPServerConfig, MCPServersConfig
from jarvis.mcp.manager import MCPManager
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
            assert servers[0].status in ("disconnected", "error")
            assert servers[0].last_error is not None
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
