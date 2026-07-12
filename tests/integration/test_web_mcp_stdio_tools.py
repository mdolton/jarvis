import asyncio
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
    yield TestClient(app, headers={"origin": "http://testserver"}), factory, ctx
    await engine.dispose()


def test_allow_all_sets_every_tool_and_clears_cache(client_and_factory):
    client, factory, ctx = client_and_factory

    resp = client.post("/mcp/stdio/brave/tools/allow-all", follow_redirects=False)
    assert resp.status_code == 303

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


def test_stdio_section_renders_tools_table_and_allow_all(client_and_factory):
    client, _factory, _ctx = client_and_factory
    page = client.get("/mcp").text
    # per-tool policy form for a stdio tool
    assert "brave_web_search" in page
    assert 'data-policy-tool="brave_web_search"' in page
    # bulk allow-all button for the server
    assert 'action="/mcp/stdio/brave/tools/allow-all"' in page
