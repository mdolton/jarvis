import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    # Seed an MCP server + tool.
    async with factory() as s:
        server = await MCPServerRepo(s).upsert(name="gcal", transport="stdio")
        await MCPServerRepo(s).set_status(server.id, status="connected", last_error=None)
        await MCPToolRepo(s).replace_for_server(
            server.id,
            tools=[MCPToolDescriptor(name="list_events", input_schema={}, read_only_hint=True)],
        )

    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.session_factory = factory

    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_mcp_page_renders_server_and_tools(client):
    resp = client.get("/mcp")
    assert resp.status_code == 200
    assert "gcal" in resp.text
    assert "list_events" in resp.text
    assert "connected" in resp.text.lower()
